import io
import logging
from time import time
from ssl import (
    SSLSocket,
    create_default_context
)
from typing import (
    Any,
    Iterable,
    Callable,
    Generator,
    ParamSpec,
    TypeVar,
)
from socket import (
    socket,
    AF_INET,
    SOCK_STREAM,
)
from select import select
from collections import deque
from functools import partial
# pip install wxPython
import wx 
# pip install h2
import h2.connection
import h2.events
# pip install certifi
import certifi


P = ParamSpec("P")
T = TypeVar("T")

IMG_DEM = 128 # 128x128 images
SHOW = 16 # show 16 images total
PER_ROW = 4 # show 4 images per row
g_shutdown = False

logging.basicConfig(level=logging.DEBUG)


def set_shudown(_):
    global g_shutdown
    g_shutdown = True

class Scheduler():

    def __init__(self):
        self.tasks = deque[Generator[Any, Any, Any]]()
        self.task_q_locked = False

    def lock(self):
        self.task_q_locked = True

    def unlock(self):
        self.task_q_locked = False

    def coroutine(self, func: Callable[P, Generator[Any, Any, T]]):
        def inner(*args: P.args, **kwargs: P.kwargs) -> Generator[Any, Any, T]:
            self.lock()
            gen = func(*args, **kwargs)
            self.tasks.append(gen)
            self.unlock()
            return gen
        return inner

    def tick(self):
        global g_shutdown
        if not len(self.tasks):
            return
        if self.task_q_locked:
            return
        self.lock()
        gen = self.tasks.pop()
        top_priority = True
        try:
            if g_shutdown:
                gen.throw(StopIteration)
            value = next(gen)
            if isinstance(value, int) and value == 1:
                top_priority = False
        except StopIteration:
            return
        else:
            (self.tasks.appendleft
            if top_priority else
            self.tasks.append
            )(gen)
        finally:
            self.unlock()

scheduler = Scheduler()

@scheduler.coroutine
def sleep(s: int | float, bmid: int):
    logging.debug("[ID %i] Sleeping for %.2f seconds...", bmid, s)
    end = time() + s
    while time() <= end:
        yield 1
    logging.debug("ID [%i] Woke up", bmid)
    return True

@scheduler.coroutine
def wait_for_select(rlist: list, wlist: list, xlist: list, bmid: int):
    while True:
        r, w, x = select(rlist, wlist, xlist, 0)
        if rlist and r == rlist:
            break
        if wlist and w == wlist:
            break
        if xlist and x == xlist:
            break
        yield 1
    return
        

@scheduler.coroutine
def get_conn(addr: tuple[str, int], reqid: int):
    yield
    ctx = create_default_context(cafile=certifi.where())
    ctx.set_alpn_protocols(['h2'])
    conn = socket(AF_INET, SOCK_STREAM)
    conn.setblocking(True)
    conn.connect(addr)
    conn = ctx.wrap_socket(conn, server_hostname=addr[0])
    yield
    logging.info("[ID %i] Prepairing connection...", reqid)
    return conn

@scheduler.coroutine
def connect(
    conn: SSLSocket, 
    h2conn: h2.connection.H2Connection,
    headers: Iterable[tuple[str, str]],
    reqid: int
):
    h2conn.initiate_connection()
    yield from wait_for_select([], [conn], [], reqid)
    conn.sendall(h2conn.data_to_send())
    h2conn.send_headers(1, headers, end_stream=True)
    yield from wait_for_select([], [conn], [], reqid)
    conn.sendall(h2conn.data_to_send())
    logging.info("[ID %i] Successfully connected!", reqid)
    yield

@scheduler.coroutine
def request(
    addr: tuple[str,int],
    headers: Iterable[tuple[str, str]],
    bm: wx.StaticBitmap,
    reqid: int,
    buf: bytearray
):
    yield from draw_bitmap(
        bm,
        f"ID {reqid}\n0/4 [____]\n\nPrepairing...", reqid)
    conn = yield from get_conn(addr, reqid)
    h2conn = h2.connection.H2Connection()
    yield from draw_bitmap(
        bm,
        f"ID {reqid}\n1/4 [█___]\n\nRequesting...", reqid)
    # throttled on purpose 
    yield from sleep(0.5, reqid)
    yield from connect(conn, h2conn, headers, reqid)
    done = False
    reported = False
    yield 
    while not done:
        yield from wait_for_select([conn],[],[], reqid)
        data = conn.recv(4096)
        if not data:
            done = True
            break
        yield
        events = h2conn.receive_data(data)
        for event in events:
            if isinstance(event, h2.events.ResponseReceived):
                logging.info("[ID %i] Recieved response!", reqid)
                yield from draw_bitmap(bm, f"ID {reqid}\n2/4 [██__]\n\nGot response!", reqid)
            if isinstance(event, h2.events.DataReceived):
                h2conn.acknowledge_received_data(
                    event.flow_controlled_length, # type: ignore
                    event.stream_id # type: ignore
                )
                buf.extend(event.data or b"")
                if not reported:
                    yield from draw_bitmap(bm, f"ID {reqid}\n3/4 [███_]\n\nGathering...", reqid)
                    reported = True
            if isinstance(event, h2.events.StreamEnded):
                logging.info("[ID %i] Stream ended", reqid)
                draw_bitmap(bm, f"ID {reqid}\n4/4 [████]\n\nProcessing...", reqid)
                done = True
                break
            yield
        yield from wait_for_select([],[conn],[], reqid)
        conn.sendall(h2conn.data_to_send())
    yield
    h2conn.close_connection()
    yield from wait_for_select([],[conn],[], reqid)
    conn.sendall(h2conn.data_to_send())
    conn.close()
    logging.info("[ID %i] Closed the connection", reqid)
    yield

@scheduler.coroutine
def draw_bitmap(bm: wx.StaticBitmap, text: str, reqid: int):
    logging.info("[ID %i] Updating bitmap", reqid)
    memdc = wx.MemoryDC()
    t_bm = wx.Bitmap(IMG_DEM, IMG_DEM)
    memdc.SelectObject(t_bm)
    # set white bg
    memdc.SetBackground(wx.Brush(wx.WHITE))
    memdc.Clear()
    # set font
    font = wx.Font(
        20,
        wx.FONTFAMILY_DEFAULT,
        wx.FONTSTYLE_NORMAL,
        wx.FONTWEIGHT_BOLD
    )
    memdc.SetFont(font)
    memdc.SetTextForeground(wx.BLACK)
    # draw
    memdc.DrawText(text, wx.Point(3,3))
    # set the temp bitmap
    bm.SetBitmap(t_bm) # type: ignore
    memdc.SelectObject(wx.NullBitmap)
    yield


@scheduler.coroutine
def refresh_image(img: wx.StaticBitmap, reqid: int):
    buf = bytearray()
    yield from request((
        "thispersondoesnotexist.com", 443), [
            (':method', 'GET'),
            (':path', '/'),
            (':authority', "thispersondoesnotexist.com"),
            (':scheme', 'https'),
        ], img, reqid, buf
    )
    yield
    buf = io.BytesIO(buf)
    im = wx.Image(stream=buf).Scale(IMG_DEM, IMG_DEM) # type: ignore
    bm = wx.Bitmap(im)
    img.SetBitmap(bm) # type: ignore
    logging.info("[ID %i] Successfully updated the image. Done", reqid)
    yield


def refresh_images(img_grid: list, event: object):
    logging.info("Scheduling the global refresh...")
    for img in img_grid:
        refresh_image(img, img_grid.index(img))

def get_offset(add: int):
    return (SHOW//PER_ROW)*(IMG_DEM+add)

def render_ui(panel: wx.Panel):
    button = wx.Button(**{
        "parent": panel,
        "label":"Refresh",
        "pos": wx.Point(10, get_offset(20))
    })

    empty_bm = wx.Bitmap(IMG_DEM, IMG_DEM)
    img_grid = [
        wx.StaticBitmap(
            panel, i,
            empty_bm, # type: ignore
            wx.Point(*map(
                lambda i: i*(20+IMG_DEM),
                divmod(i, PER_ROW))))
        for i in range(SHOW)
    ]

    button.Bind(
        wx.EVT_BUTTON,
        partial(refresh_images, img_grid)
    )
    refresh_images(img_grid, None)

def main():
    global g_shutdown
    app = wx.App()
    frame = wx.Frame(
        None,
        title="Those people do not exist!",
        size=wx.Size(
            get_offset(20),
            get_offset(40)
        )
    )
    panel = wx.Panel(frame)
    frame.Bind(wx.EVT_CLOSE, set_shudown)
    render_ui(panel)
    frame.Show()
    try:
        while not g_shutdown:
            app.Yield()
            scheduler.tick()
    except KeyboardInterrupt:
        set_shudown(None)
    finally:
        frame.Destroy()
        quit(0)
