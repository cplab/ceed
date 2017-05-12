from multiprocessing import Process, Queue
import os
import sys
import traceback
from collections import defaultdict
try:
    from Queue import Empty
except ImportError:
    from queue import Empty

from kivy.event import EventDispatcher
from kivy.properties import NumericProperty, StringProperty, BooleanProperty, \
    ObjectProperty
from kivy.clock import Clock
from kivy.compat import clock
from kivy.graphics import Color, Point, Fbo, Rectangle
from kivy.graphics.texture import Texture
from kivy.app import App
from kivy.uix.behaviors.knspace import knspace

from cplcom.app import app_error
from cplcom.utils import json_dumps, json_loads

import ceed
from ceed.stage import StageFactory, StageDoneException
from ceed.storage.controller import DataSerializer, CeedData

if ceed.has_gui_control or ceed.is_view_inst:
    from kivy.core.window import Window

try:
    from pypixxlib import _libdpx as libdpx
    from pypixxlib.propixx import PROPixx
except ImportError:
    libdpx = PROPixx = None


class ViewControllerBase(EventDispatcher):

    __settings_attrs__ = (
        'screen_width', 'screen_height', 'frame_rate',
        'use_software_frame_rate', 'cam_scale', 'cam_offset_x', 'cam_offset_y',
        'cam_rotation', 'output_count', 'screen_offset_x', 'preview',
        'fullscreen', 'video_mode', 'LED_mode')

    screen_width = NumericProperty(1920)

    screen_height = NumericProperty(1080)

    screen_offset_x = NumericProperty(0)

    frame_rate = NumericProperty(60.)

    use_software_frame_rate = BooleanProperty(True)

    cam_scale = NumericProperty(1.)

    cam_offset_x = NumericProperty(0)

    cam_offset_y = NumericProperty(0)

    cam_rotation = NumericProperty(0)

    output_count = BooleanProperty(True)

    preview = BooleanProperty(True)

    fullscreen = BooleanProperty(False)

    stage_active = BooleanProperty(False)

    cpu_fps = NumericProperty(0)

    gpu_fps = NumericProperty(0)

    propixx_lib = BooleanProperty(False)

    video_modes = ['RGB', 'RB3D', 'RGB240', 'RGB180', 'QUAD4X', 'QUAD12X',
                   'GREY3X']

    led_modes = {'RGB': 0, 'GB': 1, 'RB': 2, 'B': 3, 'RG': 4, 'G': 5, 'R': 6,
                 'none': 7}

    video_mode = StringProperty('RGB')

    LED_mode = StringProperty('RGB')

    do_quad_mode = False
    '''If quad mode, we just quadruple the window size.
    '''

    _original_fps = Clock._max_fps if not os.environ.get(
        'KIVY_DOC_INCLUDE', None) else 0

    canvas_name = 'view_controller'

    current_canvas = None

    shape_views = []

    quad_fbos = []

    tick_event = None

    tick_func = None

    count = 0

    _cpu_stats = {'last_call_t': 0, 'count': 0, 'tstart': 0}

    _flip_stats = {'last_call_t': 0, 'dt': []}

    flip_fps = 0

    serializer = None

    serializer_tex = None

    queue_view_read = None

    queue_view_write = None

    __events__ = ('on_changed', )

    def __init__(self, **kwargs):
        super(ViewControllerBase, self).__init__(**kwargs)
        for name in ViewControllerBase.__settings_attrs__:
            self.fbind(name, self.dispatch, 'on_changed')
        self.propixx_lib = libdpx is not None
        self.quad_fbos = []
        self.shape_views = []

    def on_changed(self, *largs):
        pass

    def save_state(self):
        d = {}
        for name in ViewControllerBase.__settings_attrs__:
            d[name] = getattr(self, name)
        return d

    def set_state(self, data):
        for name, value in data.items():
            setattr(self, name, value)

    def request_process_data(self, data_type, data):
        pass

    def add_graphics(self, canvas):
        StageFactory.remove_shapes_gl_color_instructions(
            canvas, self.canvas_name)
        self.shape_views = []

        corners = ((0, 1), (1, 1), (0, 0), (1, 0))
        w, h = self.screen_width, self.screen_height

        if self.do_quad_mode:
            if not self.quad_fbos:
                for (x, y) in corners:
                    with canvas:
                        fbo = Fbo(size=(w, h), group=self.canvas_name)
                        Rectangle(
                            pos=(x * w, y * h), size=(w, h),
                            texture=fbo.texture, group=self.canvas_name)
                        self.quad_fbos.append(fbo)

            for fbo in self.quad_fbos:
                instructs = StageFactory.get_shapes_gl_color_instructions(
                    fbo, self.canvas_name)
                self.shape_views.append(instructs)
        else:
            self.shape_views = [StageFactory.get_shapes_gl_color_instructions(
                canvas, self.canvas_name)]

        if self.output_count and not self.serializer_tex:
            with canvas:
                Color(0, 0, 0, 1, group=self.canvas_name)
                tex = self.serializer_tex = Texture.create(size=(1, 1))
                tex.mag_filter = 'nearest'
                tex.min_filter = 'nearest'
                Rectangle(texture=tex, pos=(0, h - 1), size=(1, 1),
                          group=self.canvas_name)

    def get_all_shape_values(self, stage_name, frame_rate):
        tick = StageFactory.tick_stage(stage_name)
        frame_rate = float(frame_rate)

        obj_values = defaultdict(list)
        count = 0
        while True:
            count += 1

            try:
                tick.next()
                shape_values = tick.send(count / frame_rate)
            except StageDoneException:
                break

            values = StageFactory.fill_shape_gl_color_values(
                None, shape_values)
            for name, r, g, b, a in values:
                obj_values[name].append((r, g, b, a))
        return obj_values

    def start_stage(self, stage_name, canvas):
        if self.tick_event:
            raise TypeError('Cannot start new stage while stage is active')

        Clock._max_fps = 0
        self.tick_event = Clock.schedule_interval(self.tick_callback, 0)
        Window.fbind('on_flip', self.flip_callback)

        self.current_canvas = canvas
        self.tick_func = StageFactory.tick_stage(stage_name)

        self._flip_stats['last_call_t'] = self._cpu_stats['last_call_t'] = \
            self._cpu_stats['tstart'] = clock()

        if self.output_count:
            kwargs = App.get_running_app().app_settings['serializer']
            self.serializer = DataSerializer(**kwargs).get_bits(-1)

        self.add_graphics(canvas)

    def end_stage(self):
        if not self.tick_event:
            return

        self.tick_event.cancel()
        Window.funbind('on_flip', self.flip_callback)
        Clock._max_fps = self._original_fps
        StageFactory.remove_shapes_gl_color_instructions(
            self.current_canvas, self.canvas_name)

        self.tick_func = self.tick_event = self.current_canvas = None
        self.shape_views = []
        self.count = 0
        self._cpu_stats['count'] = 0
        del self._flip_stats['dt'][:]

        self.serializer_tex = None
        self.serializer = None

    def tick_callback(self, *largs):
        t = clock()
        stats = self._cpu_stats
        tdiff = t - stats['last_call_t']
        rate = float(self.frame_rate)

        stats['count'] += 1
        if t - stats['tstart'] >= 1:
            fps = stats['count'] / (t - stats['tstart'])
            self.request_process_data('CPU', fps)
            stats['tstart'] = t
            stats['count'] = 0

        if self.use_software_frame_rate and tdiff < 1 / rate:
            return

        stats['last_call_t'] = t

        tick = self.tick_func
        if self.video_mode == 'QUAD4X':
            projections = [None, ] * 4
            views = self.shape_views
        elif self.video_mode == 'QUAD12X':
            projections = (['r', ] * 4) + (['g', ] * 4) + (['b', ] * 4)
            views = [view for _ in range(4) for view in self.shape_views]
        else:
            projections = [None, ]
            views = self.shape_views

        bits = None if self.serializer else 0
        for shape_views, proj in zip(views, projections):
            self.count += 1

            try:
                tick.next()
                shape_values = tick.send(self.count / self.frame_rate)
            except StageDoneException:
                self.end_stage()
                return
            except Exception:
                self.end_stage()
                raise

            if self.serializer and bits is None:
                self.serializer.next()
                bits = self.serializer.send(self.count)
                r, g, b = bits & 0xFF, (bits & 0xFF00) >> 8, \
                    (bits & 0xFF0000) >> 16
                self.serializer_tex.blit_buffer(
                    bytearray([r, g, b]), colorfmt='rgb', bufferfmt='ubyte')

            values = StageFactory.fill_shape_gl_color_values(
                shape_views, shape_values, proj)
            self.request_process_data('frame', (self.count, bits, values))

    def flip_callback(self, *largs):
        Window.on_flip()

        t = clock()
        self.request_process_data('frame_flip', (self.count, t))

        stats = self._flip_stats
        tdiff = t - stats['last_call_t']
        rate = float(self.frame_rate)

        stats['dt'].append(tdiff)
        stats['last_call_t'] = t
        if len(stats['dt']) >= rate:
            fps = self.flip_fps = len(stats['dt']) / sum(stats['dt'])
            self.request_process_data('GPU', fps)
            del stats['dt'][:]
        return True


class ViewSideViewControllerBase(ViewControllerBase):

    def start_stage(self, stage_name, canvas):
        self.prepare_view_window()
        return super(ViewSideViewControllerBase, self).start_stage(
            stage_name, canvas)

    def end_stage(self):
        val = super(ViewSideViewControllerBase, self).end_stage()
        self.queue_view_write.put_nowait(('end_stage', None))
        return val

    def request_process_data(self, data_type, data):
        self.queue_view_write.put_nowait((data_type, json_dumps(data)))

    def view_process_enter(self, read, write, settings, app_settings):
        def assign_settings(*largs):
            App.get_running_app().app_settings = app_settings
        for k, v in settings.items():
            setattr(self, k, v)
        self.do_quad_mode = self.video_mode.startswith('QUAD')
        from ceed.view.main import run_app
        self.queue_view_read = read
        self.queue_view_write = write
        Clock.schedule_once(assign_settings, 0)
        Clock.schedule_interval(self.view_read, .25)
        Clock.schedule_once(self.prepare_view_window, 0)

        try:
            run_app()
        except Exception as e:
            App.get_running_app().handle_exception(e, exc_info=sys.exc_info())

        self.queue_view_write.put_nowait(('eof', None))

    def handle_exception(self, exception, exc_info=None):
        if exc_info is not None:
            exc_info = ''.join(traceback.format_exception(*exc_info))
        self.queue_view_write.put_nowait(
            ('exception', json_dumps(exception, exc_info)))

    @app_error
    def view_read(self, *largs):
        read = self.queue_view_read
        write = self.queue_view_write
        while True:
            try:
                msg, value = read.get(False)
                if msg == 'eof':
                    App.get_running_app().stop()
                    break
                elif msg == 'config':
                    if self.tick_event:
                        raise Exception('Cannot configure while running stage')
                    CeedData.clear_existing_config_data()
                    CeedData.apply_config_data_dict(json_loads(value))
                elif msg == 'start_stage':
                    self.start_stage(value, App.get_running_app().root.canvas)
                elif msg == 'end_stage':
                    self.end_stage()
                elif msg == 'fullscreen':
                    Window.fullscreen = self.fullscreen = value
                write.put_nowait(('response', msg))
            except Empty:
                break

    def prepare_view_window(self, *largs):
        if Window.fullscreen != self.fullscreen or not self.fullscreen:
            Window.maximize()
            if self.do_quad_mode:
                Window.size = 2 * self.screen_width, 2 * self.screen_height
            else:
                Window.size = self.screen_width, self.screen_height
            Window.left = self.screen_offset_x
            Window.fullscreen = self.fullscreen


def view_process_enter(*largs):
    return ViewController.view_process_enter(*largs)


class ControllerSideViewControllerBase(ViewControllerBase):

    view_process = ObjectProperty(None, allownone=True)

    def request_stage_start(self, stage_name):
        if not stage_name:
            raise ValueError('No stage specified')

        if self.preview:
            CeedData.prepare_experiment(stage_name)
            self.start_stage(stage_name, knspace.painter.canvas)
            self.stage_active = True
        elif self.view_process:
            CeedData.prepare_experiment(stage_name)
            self.queue_view_read.put_nowait(
                ('config', json_dumps(CeedData.gather_config_data_dict())))
            self.queue_view_read.put_nowait(('start_stage', stage_name))
            self.stage_active = True
        else:
            raise Exception("No window to run experiment")

    def request_stage_end(self):
        if self.preview:
            self.end_stage()
            CeedData.stop_experiment()
            self.stage_active = False
        elif self.view_process:
            self.queue_view_read.put_nowait(('end_stage', None))

    def end_stage(self):
        val = super(ControllerSideViewControllerBase, self).end_stage()
        self.stage_active = False
        return val

    def request_fullscreen(self, state):
        self.fullscreen = state
        if self.view_process:
            self.queue_view_read.put_nowait(('fullscreen', state))

    def request_process_data(self, data_type, data):
        if data_type == 'GPU':
            self.gpu_fps = data
        elif data_type == 'CPU':
            self.cpu_fps = data
        elif data_type == 'frame':
            CeedData.add_frame(*data)
        elif data_type == 'frame_flip':
            if data[0]:  # counts of zero is too early
                CeedData.add_frame_flip(*data)

    def start_process(self):
        if self.view_process:
            return

        settings = {name: getattr(self, name)
                    for name in ViewControllerBase.__settings_attrs__}
        r = self.queue_view_read = Queue()
        w = self.queue_view_write = Queue()
        os.environ['CEED_IS_VIEW'] = '1'
        self.view_process = process = Process(
            target=view_process_enter,
            args=(r, w, settings, App.get_running_app().app_settings))
        process.start()
        del os.environ['CEED_IS_VIEW']
        Clock.schedule_interval(self.controller_read, .25)

    def stop_process(self):
        if self.view_process:
            self.queue_view_read.put_nowait(('eof', None))

    def finish_stop_process(self):
        '''Can only be called after recieving eof from view side.
        '''
        if not self.view_process:
            return

        self.view_process.join()
        self.view_process = self.queue_view_read = self.queue_view_write = None
        Clock.unschedule(self.controller_read)

    def controller_read(self, *largs):
        write = self.queue_view_read
        read = self.queue_view_write
        while True:
            try:
                msg, value = read.get(False)
                if msg == 'eof':
                    self.finish_stop_process()
                    CeedData.stop_experiment()
                    self.stage_active = False
                    break
                elif msg == 'exception':
                    e, exec_info = json_loads(value)
                    App.get_running_app().handle_exception(
                        e, exc_info=exec_info)
                elif msg in ('GPU', 'CPU', 'frame', 'frame_flip'):
                    self.request_process_data(msg, json_loads(value))
                elif msg == 'response' and value == 'end_stage':
                    self.stage_active = False
                elif msg == 'end_stage':
                    CeedData.stop_experiment()
                    self.stage_active = False
            except Empty:
                break

    @app_error
    def set_led_mode(self, mode):
        if libdpx is None:
            raise ImportError('Cannot open PROPixx library')
        libdpx.DPxOpen()
        libdpx.DPxSetPPxLedMask(self.led_modes[mode])
        libdpx.DPxUpdateRegCache()
        libdpx.DPxClose()
        self.LED_mode = mode

    @app_error
    def set_video_mode(self, mode):
        if PROPixx is None:
            raise ImportError('Cannot open PROPixx library')
        dev = PROPixx()
        dev.setDlpSequencerProgram(mode)
        dev.updateRegisterCache()
        dev.close()
        self.video_mode = mode

def process_enter(*largs, **kwargs):
    ViewController.view_process_enter(*largs, **kwargs)

if ceed.is_view_inst:
    ViewController = ViewSideViewControllerBase()
else:
    ViewController = ControllerSideViewControllerBase()
