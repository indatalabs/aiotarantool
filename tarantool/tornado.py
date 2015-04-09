# -*- coding: utf-8 -*-

# tarantool client for tornado

import socket
import errno
import msgpack
import base64

import tarantool
from tarantool.response import Response
from tarantool.request import (
    Request,
    RequestCall,
    RequestDelete,
    RequestEval,
    RequestInsert,
    RequestJoin,
    RequestReplace,
    RequestPing,
    RequestSelect,
    RequestSubscribe,
    RequestUpdate,
    RequestAuthenticate)

from tarantool.schema import SchemaIndex, SchemaSpace
from tarantool.error import SchemaError
import tarantool.const
from tarantool.utils import check_key

from tarantool.error import (
    NetworkError,
    DatabaseError,
    warn,
    RetryWarning)

from tarantool.const import (
    REQUEST_TYPE_OK,
    REQUEST_TYPE_ERROR,
    RETRY_MAX_ATTEMPTS,
    IPROTO_GREETING_SIZE)

from tornado.ioloop import IOLoop
import tornado.gen
import tornado.concurrent
import tornado.locks
import tornado.tcpclient

import logging
logger = logging.getLogger(__package__)


def connect(host, post, user=None, password=None, loop=None):
    conn = Connection(host, post, user=user, password=password, loop=loop)

    return conn


class Schema(object):
    def __init__(self, con):
        self.schema = {}
        self.con = con

    @tornado.gen.coroutine
    def get_space(self, space):
        try:
            return self.schema[space]
        except KeyError:
            pass

        if not self.con.connected:
            yield self.con.connect()

        with (yield self.con.lock.acquire()):
            if space in self.schema:
                return self.schema[space]

            _index = (tarantool.const.INDEX_SPACE_NAME
                      if isinstance(space, str)
                      else tarantool.const.INDEX_SPACE_PRIMARY)

            array = yield self.con.select(tarantool.const.SPACE_SPACE, space, index=_index)
            if len(array) > 1:
                raise SchemaError('Some strange output from server: \n' + array)

            if len(array) == 0 or not len(array[0]):
                temp_name = ('name' if isinstance(space, str) else 'id')
                raise SchemaError(
                    "There's no space with {1} '{0}'".format(space, temp_name))

            array = array[0]
            return SchemaSpace(array, self.schema)

    @tornado.gen.coroutine
    def get_index(self, space, index):
        _space = yield self.get_space(space)
        try:
            return _space.indexes[index]
        except KeyError:
            pass

        if not self.con.connected:
            yield self.con.connect()

        with (yield self.con.lock.acquire()):
            if index in _space.indexes:
                return _space.indexes[index]

            _index = (tarantool.const.INDEX_INDEX_NAME
                      if isinstance(index, str)
                      else tarantool.const.INDEX_INDEX_PRIMARY)

            array = yield self.con.select(tarantool.const.SPACE_INDEX, [_space.sid, index], index=_index)

            if len(array) > 1:
                raise SchemaError('Some strange output from server: \n' + array)

            if len(array) == 0 or not len(array[0]):
                temp_name = ('name' if isinstance(index, str) else 'id')
                raise SchemaError(
                    "There's no index with {2} '{0}' in space '{1}'".format(
                        index, _space.name, temp_name))

            array = array[0]
            return SchemaIndex(array, _space)

    def flush(self):
        self.schema.clear()


class Connection(tarantool.Connection):
    DatabaseError = DatabaseError

    def __init__(self, host, port, user=None, password=None, connect_now=False, loop=None,
                 buffer_size=16384 * 2):
        """just create instance, do not really connect by default"""

        super(Connection, self).__init__(host, port,
                                         user=user,
                                         password=password,
                                         connect_now=connect_now)

        self.buffer_size = buffer_size
        assert isinstance(self.buffer_size, int)

        self.loop = loop or IOLoop.current()
        self.lock = tornado.locks.Semaphore()  # event loop ?
        self._tcp_client = tornado.tcpclient.TCPClient()  # event loop ?
        self.stream = None

        self.connect_now = connect_now
        self.connected = False
        self.req_num = 0

        self._waiters = dict()
        self._reader_task = None
        self._writer_task = None
        self._write_event = None
        self._write_buf = None

        self.error = False  # important not raise exception in response reader
        self.schema = Schema(self)  # need schema with lock

    @tornado.gen.coroutine
    def connect(self):
        if self.connected:
            return

        with (yield self.lock.acquire()):
            if self.connected:
                return

            logger.debug("connecting to %r" % self)
            self.stream = yield self._tcp_client.connect(self.host, self.port, max_buffer_size=self.buffer_size)
            self._write_event = tornado.locks.Event()
            self._write_buf = b""
            self.connected = True

            self._reader_task = tornado.gen.Task(self._response_reader)
            self._writer_task = tornado.gen.Task(self._response_writer)

        if self.user and self.password:
            yield self.authenticate(self.user, self.password)

    def generate_sync(self):
        self.req_num += 1
        if self.req_num > 10000000:
            self.req_num = 0

        self._waiters[self.req_num] = tornado.concurrent.Future()
        return self.req_num

    @tornado.gen.coroutine
    def close(self):
        yield self._do_close(None)

    @tornado.gen.coroutine
    def _do_close(self, exc):
        if not self.connected:
            return

        with (yield self.lock.acquire()):
            self.connected = False
            yield self._tcp_client.close()
            self._reader_task.cancel()
            self._reader_task = None

            self._writer_task.cancel()
            self._writer_task = None
            self._write_event = None
            self._write_buf = None

            for waiter in self._waiters.values():
                if exc is None:
                    waiter.cancel()
                else:
                    waiter.set_exception(exc)

            self._waiters = dict()

    def __repr__(self):
        return "tarantool.tornado.Connection(host=%r, port=%r)" % (self.host, self.port)

    @tornado.gen.coroutine
    def _response_writer(self):
        while self.connected:
            yield self._write_event.wait()

            if self._write_buf:
                to_write = self._write_buf
                self._write_buf = b""
                yield self.stream.write(to_write)

            self._write_event.clear()

    @tornado.gen.coroutine
    def _response_reader(self):
        # handshake
        greeting = yield self.stream.read_bytes(IPROTO_GREETING_SIZE)
        self._salt = base64.decodestring(greeting[64:])[:20]

        buf = b""
        while self.connected:
            tmp_buf = yield self.stream.read_bytes(self.buffer_size, partial=True)
            if not tmp_buf:
                yield self._do_close(
                    NetworkError(socket.error(errno.ECONNRESET, "Lost connection to server during query")))

            buf += tmp_buf
            len_buf = len(buf)
            curr = 0

            while len_buf - curr >= 5:
                length_pack = buf[curr:curr + 5]
                length = msgpack.unpackb(length_pack)

                if len_buf - curr < 5 + length:
                    break

                body = buf[curr + 5:curr + 5 + length]
                curr += 5 + length

                response = Response(self, body)  # unpack response

                sync = response.sync
                if sync not in self._waiters:
                    logger.error("git happens: {r}", response)
                    continue

                waiter = self._waiters[sync]
                if response.return_code != 0:
                    waiter.set_exception(DatabaseError(response.return_code, response.return_message))
                else:
                    waiter.set_result(response)

                del self._waiters[sync]

            # one cut for buffer
            if curr:
                buf = buf[curr:]

        yield self._do_close(None)

    @tornado.gen.coroutine
    def _send_request(self, request):
        assert isinstance(request, Request)

        if not self.connected:
            yield self.connect()

        sync = request.sync
        for attempt in range(RETRY_MAX_ATTEMPTS):
            waiter = self._waiters[sync]

            self._write_buf += bytes(request)
            self._write_event.set()

            # read response
            response = yield waiter

            if response.completion_status != 1:
                return response

            self._waiters[sync] = tornado.concurrent.Future()
            warn(response.return_message, RetryWarning)

        # Raise an error if the maximum number of attempts have been made
        raise DatabaseError(response.return_code, response.return_message)

    @tornado.gen.coroutine
    def authenticate(self, user, password):
        self.user = user
        self.password = password

        if not self.connected:
            yield self.connect()

        resp = yield self._send_request(RequestAuthenticate(self, self._salt, self.user, self.password))
        return resp

    @tornado.gen.coroutine
    def insert(self, space_name, values):
        if isinstance(space_name, str):
            sp = yield self.schema.get_space(space_name)
            space_name = sp.sid

        res = yield self._send_request(RequestInsert(self, space_name, values))
        return res

    @tornado.gen.coroutine
    def select(self, space_name, key=None, **kwargs):
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 0xffffffff)
        index_name = kwargs.get("index", 0)
        iterator_type = kwargs.get("iterator", 0)

        key = check_key(key, select=True)

        if isinstance(space_name, str):
            sp = yield self.schema.get_space(space_name)
            space_name = sp.sid

        if isinstance(index_name, str):
            idx = yield self.schema.get_index(space_name, index_name)
            index_name = idx.iid

        res = yield self._send_request(
            RequestSelect(self, space_name, index_name, key, offset, limit, iterator_type))

        return res

    @tornado.gen.coroutine
    def update(self, space_name, key, op_list, **kwargs):
        index_name = kwargs.get("index", 0)

        key = check_key(key)
        if isinstance(space_name, str):
            sp = yield self.schema.get_space(space_name)
            space_name = sp.sid

        if isinstance(index_name, str):
            idx = yield self.schema.get_index(space_name, index_name)
            index_name = idx.iid

        res = yield self._send_request(
            RequestUpdate(self, space_name, index_name, key, op_list))

        return res

    @tornado.gen.coroutine
    def delete(self, space_name, key, **kwargs):
        index_name = kwargs.get("index", 0)

        key = check_key(key)
        if isinstance(space_name, str):
            sp = yield self.schema.get_space(space_name)
            space_name = sp.sid

        if isinstance(index_name, str):
            idx = yield self.schema.get_index(space_name, index_name)
            index_name = idx.iid

        res = yield self._send_request(
            RequestDelete(self, space_name, index_name, key))

        return res