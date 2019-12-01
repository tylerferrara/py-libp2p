import asyncio
import logging
import math
from typing import Any  # noqa: F401
from typing import Awaitable, Dict, List, Optional, Tuple

from async_service import Service
import trio

from libp2p.exceptions import ParseError
from libp2p.io.exceptions import IncompleteReadError
from libp2p.network.connection.exceptions import RawConnError
from libp2p.peer.id import ID
from libp2p.security.secure_conn_interface import ISecureConn
from libp2p.stream_muxer.abc import IMuxedConn, IMuxedStream
from libp2p.typing import TProtocol
from libp2p.utils import (
    decode_uvarint_from_stream,
    encode_uvarint,
    encode_varint_prefixed,
    read_varint_prefixed_bytes,
)

from .constants import HeaderTags
from .datastructures import StreamID
from .exceptions import MplexUnavailable
from .mplex_stream import MplexStream

MPLEX_PROTOCOL_ID = TProtocol("/mplex/6.7.0")

logger = logging.getLogger("libp2p.stream_muxer.mplex.mplex")


class Mplex(IMuxedConn, Service):
    """
    reference: https://github.com/libp2p/go-mplex/blob/master/multiplex.go
    """

    secured_conn: ISecureConn
    peer_id: ID
    next_channel_id: int
    streams: Dict[StreamID, MplexStream]
    streams_lock: trio.Lock
    streams_msg_channels: Dict[StreamID, "trio.MemorySendChannel[bytes]"]
    new_stream_send_channel: "trio.MemorySendChannel[IMuxedStream]"
    new_stream_receive_channel: "trio.MemoryReceiveChannel[IMuxedStream]"

    event_shutting_down: trio.Event
    event_closed: trio.Event

    def __init__(self, secured_conn: ISecureConn, peer_id: ID) -> None:
        """
        create a new muxed connection.

        :param secured_conn: an instance of ``ISecureConn``
        :param generic_protocol_handler: generic protocol handler
        for new muxed streams
        :param peer_id: peer_id of peer the connection is to
        """
        self.secured_conn = secured_conn

        self.next_channel_id = 0

        # Set peer_id
        self.peer_id = peer_id

        # Mapping from stream ID -> buffer of messages for that stream
        self.streams = {}
        self.streams_lock = trio.Lock()
        self.streams_msg_channels = {}
        send_channel, receive_channel = trio.open_memory_channel(math.inf)
        self.new_stream_send_channel = send_channel
        self.new_stream_receive_channel = receive_channel
        self.event_shutting_down = trio.Event()
        self.event_closed = trio.Event()

    async def run(self):
        self.manager.run_task(self.handle_incoming)
        await self.manager.wait_finished()

    @property
    def is_initiator(self) -> bool:
        return self.secured_conn.is_initiator

    async def close(self) -> None:
        """close the stream muxer and underlying secured connection."""
        if self.event_shutting_down.is_set():
            return
        # Set the `event_shutting_down`, to allow graceful shutdown.
        self.event_shutting_down.set()
        await self.secured_conn.close()
        # Blocked until `close` is finally set.
        await self.event_closed.wait()

    def is_closed(self) -> bool:
        """
        check connection is fully closed.

        :return: true if successful
        """
        return self.event_closed.is_set()

    def _get_next_channel_id(self) -> int:
        """
        Get next available stream id.

        :return: next available stream id for the connection
        """
        next_id = self.next_channel_id
        self.next_channel_id += 1
        return next_id

    async def _initialize_stream(self, stream_id: StreamID, name: str) -> MplexStream:
        # Use an unbounded buffer, to avoid `handle_incoming` being blocked when doing
        # `send_channel.send`.
        send_channel, receive_channel = trio.open_memory_channel(math.inf)
        stream = MplexStream(name, stream_id, self, receive_channel)
        async with self.streams_lock:
            self.streams[stream_id] = stream
            self.streams_msg_channels[stream_id] = send_channel
        return stream

    async def open_stream(self) -> IMuxedStream:
        """
        creates a new muxed_stream.

        :return: a new ``MplexStream``
        """
        channel_id = self._get_next_channel_id()
        stream_id = StreamID(channel_id=channel_id, is_initiator=True)
        # Default stream name is the `channel_id`
        name = str(channel_id)
        stream = await self._initialize_stream(stream_id, name)
        await self.send_message(HeaderTags.NewStream, name.encode(), stream_id)
        return stream

    async def accept_stream(self) -> IMuxedStream:
        """accepts a muxed stream opened by the other end."""
        try:
            return await self.new_stream_receive_channel.receive()
        except (trio.ClosedResourceError, trio.EndOfChannel):
            raise MplexUnavailable

    async def send_message(
        self, flag: HeaderTags, data: Optional[bytes], stream_id: StreamID
    ) -> int:
        """
        sends a message over the connection.

        :param header: header to use
        :param data: data to send in the message
        :param stream_id: stream the message is in
        """
        print(
            f"!@# send_message: {self._id}: flag={flag}, data={data}, stream_id={stream_id}"
        )
        # << by 3, then or with flag
        header = encode_uvarint((stream_id.channel_id << 3) | flag.value)

        if data is None:
            data = b""

        _bytes = header + encode_varint_prefixed(data)

        return await self.write_to_stream(_bytes)

    async def write_to_stream(self, _bytes: bytes) -> int:
        """
        writes a byte array to a secured connection.

        :param _bytes: byte array to write
        :return: length written
        """
        await self.secured_conn.write(_bytes)
        return len(_bytes)

    async def handle_incoming(self) -> None:
        """Read a message off of the secured connection and add it to the
        corresponding message buffer."""

        while self.manager.is_running:
            try:
                print(
                    f"!@# handle_incoming: {self._id}: before _handle_incoming_message"
                )
                await self._handle_incoming_message()
                print(
                    f"!@# handle_incoming: {self._id}: after _handle_incoming_message"
                )
            except MplexUnavailable as e:
                logger.debug("mplex unavailable while waiting for incoming: %s", e)
                print(f"!@# handle_incoming: {self._id}: MplexUnavailable: {e}")
                break

        print(f"!@# handle_incoming: {self._id}: leaving")
        # If we enter here, it means this connection is shutting down.
        # We should clean things up.
        await self._cleanup()

    async def read_message(self) -> Tuple[int, int, bytes]:
        """
        Read a single message off of the secured connection.

        :return: stream_id, flag, message contents
        """

        try:
            header = await decode_uvarint_from_stream(self.secured_conn)
        except (ParseError, RawConnError, IncompleteReadError) as error:
            raise MplexUnavailable(
                f"failed to read the header correctly from the underlying connection: {error}"
            )
        try:
            message = await read_varint_prefixed_bytes(self.secured_conn)
        except (ParseError, RawConnError, IncompleteReadError) as error:
            raise MplexUnavailable(
                "failed to read the message body correctly from the underlying connection: "
                f"{error}"
            )

        flag = header & 0x07
        channel_id = header >> 3

        return channel_id, flag, message

    @property
    def _id(self) -> int:
        return 0 if self.is_initiator else 1

    async def _handle_incoming_message(self) -> None:
        """
        Read and handle a new incoming message.

        :raise MplexUnavailable: `Mplex` encounters fatal error or is shutting down.
        """
        print(f"!@# _handle_incoming_message: {self._id}: before reading")
        channel_id, flag, message = await self.read_message()
        print(
            f"!@# _handle_incoming_message: {self._id}: channel_id={channel_id}, flag={flag}, message={message}"
        )
        stream_id = StreamID(channel_id=channel_id, is_initiator=bool(flag & 1))
        print(f"!@# _handle_incoming_message: {self._id}: 2")

        if flag == HeaderTags.NewStream.value:
            print(f"!@# _handle_incoming_message: {self._id}: 3")
            await self._handle_new_stream(stream_id, message)
            print(f"!@# _handle_incoming_message: {self._id}: 4")
        elif flag in (
            HeaderTags.MessageInitiator.value,
            HeaderTags.MessageReceiver.value,
        ):
            print(f"!@# _handle_incoming_message: {self._id}: 5")
            await self._handle_message(stream_id, message)
            print(f"!@# _handle_incoming_message: {self._id}: 6")
        elif flag in (HeaderTags.CloseInitiator.value, HeaderTags.CloseReceiver.value):
            print(f"!@# _handle_incoming_message: {self._id}: 7")
            await self._handle_close(stream_id)
            print(f"!@# _handle_incoming_message: {self._id}: 8")
        elif flag in (HeaderTags.ResetInitiator.value, HeaderTags.ResetReceiver.value):
            print(f"!@# _handle_incoming_message: {self._id}: 9")
            await self._handle_reset(stream_id)
            print(f"!@# _handle_incoming_message: {self._id}: 10")
        else:
            print(f"!@# _handle_incoming_message: {self._id}: 11")
            # Receives messages with an unknown flag
            # TODO: logging
            async with self.streams_lock:
                print(f"!@# _handle_incoming_message: {self._id}: 12")
                if stream_id in self.streams:
                    print(f"!@# _handle_incoming_message: {self._id}: 13")
                    stream = self.streams[stream_id]
                    await stream.reset()
            print(f"!@# _handle_incoming_message: {self._id}: 14")

    async def _handle_new_stream(self, stream_id: StreamID, message: bytes) -> None:
        async with self.streams_lock:
            if stream_id in self.streams:
                # `NewStream` for the same id is received twice...
                raise MplexUnavailable(
                    f"received NewStream message for existing stream: {stream_id}"
                )
        mplex_stream = await self._initialize_stream(stream_id, message.decode())
        try:
            await self.new_stream_send_channel.send(mplex_stream)
        except (trio.BrokenResourceError, trio.EndOfChannel):
            raise MplexUnavailable

    async def _handle_message(self, stream_id: StreamID, message: bytes) -> None:
        print(
            f"!@# _handle_message: {self._id}: stream_id={stream_id}, message={message}"
        )
        async with self.streams_lock:
            print(f"!@# _handle_message: {self._id}: 1")
            if stream_id not in self.streams:
                # We receive a message of the stream `stream_id` which is not accepted
                #   before. It is abnormal. Possibly disconnect?
                # TODO: Warn and emit logs about this.
                print(f"!@# _handle_message: {self._id}: 2")
                return
            print(f"!@# _handle_message: {self._id}: 3")
            stream = self.streams[stream_id]
            send_channel = self.streams_msg_channels[stream_id]
        async with stream.close_lock:
            print(f"!@# _handle_message: {self._id}: 4")
            if stream.event_remote_closed.is_set():
                print(f"!@# _handle_message: {self._id}: 5")
                # TODO: Warn "Received data from remote after stream was closed by them. (len = %d)"  # noqa: E501
                return
        print(f"!@# _handle_message: {self._id}: 6")
        await send_channel.send(message)
        print(f"!@# _handle_message: {self._id}: 7")

    async def _handle_close(self, stream_id: StreamID) -> None:
        print(f"!@# _handle_close: {self._id}: step=0")
        async with self.streams_lock:
            if stream_id not in self.streams:
                # Ignore unmatched messages for now.
                return
            stream = self.streams[stream_id]
            send_channel = self.streams_msg_channels[stream_id]
        print(f"!@# _handle_close: {self._id}: step=1")
        await send_channel.aclose()
        print(f"!@# _handle_close: {self._id}: step=2")
        # NOTE: If remote is already closed, then return: Technically a bug
        #   on the other side. We should consider killing the connection.
        async with stream.close_lock:
            if stream.event_remote_closed.is_set():
                return
        print(f"!@# _handle_close: {self._id}: step=3")
        is_local_closed: bool
        async with stream.close_lock:
            stream.event_remote_closed.set()
            is_local_closed = stream.event_local_closed.is_set()
        print(f"!@# _handle_close: {self._id}: step=4")
        # If local is also closed, both sides are closed. Then, we should clean up
        #   the entry of this stream, to avoid others from accessing it.
        if is_local_closed:
            async with self.streams_lock:
                if stream_id in self.streams:
                    del self.streams[stream_id]
        print(f"!@# _handle_close: {self._id}: step=5")

    async def _handle_reset(self, stream_id: StreamID) -> None:
        async with self.streams_lock:
            if stream_id not in self.streams:
                # This is *ok*. We forget the stream on reset.
                return
            stream = self.streams[stream_id]
            send_channel = self.streams_msg_channels[stream_id]
        await send_channel.aclose()
        async with stream.close_lock:
            if not stream.event_remote_closed.is_set():
                stream.event_reset.set()
                stream.event_remote_closed.set()
            # If local is not closed, we should close it.
            if not stream.event_local_closed.is_set():
                stream.event_local_closed.set()
        async with self.streams_lock:
            if stream_id in self.streams:
                del self.streams[stream_id]
                del self.streams_msg_channels[stream_id]

    async def _cleanup(self) -> None:
        if not self.event_shutting_down.is_set():
            self.event_shutting_down.set()
        async with self.streams_lock:
            for stream_id, stream in self.streams.items():
                async with stream.close_lock:
                    if not stream.event_remote_closed.is_set():
                        stream.event_remote_closed.set()
                        stream.event_reset.set()
                        stream.event_local_closed.set()
                send_channel = self.streams_msg_channels[stream_id]
                await send_channel.aclose()
            self.streams = None
        self.event_closed.set()
        await self.new_stream_send_channel.aclose()
        await self.new_stream_receive_channel.aclose()
