import imageio.v3
import numpy as np
import sys
import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide" # don't print pygame version

on_windows = os.name == 'nt'

# we spawn subprocesses to compress BMPs to PNGs and remove the BMPs
# every time we save a BMP [which is faster than saving a PNG]

def compress_and_remove(filepairs):
    for infile, outfile in zip(filepairs[0::2], filepairs[1::2]):
        pixels = imageio.v3.imread(infile)
        imageio.imwrite(outfile, pixels)
        if np.array_equal(imageio.v3.imread(outfile), pixels):
            os.unlink(infile)

if len(sys.argv)>1 and sys.argv[1] == 'compress-and-remove':
    compress_and_remove(sys.argv[2:])
    exit()

# we use a subprocess for an open file dialog since using tkinter together with pygame
# causes issues for the latter after the first use of the former

def dir_path_dialog():
    import tkinter
    import tkinter.filedialog

    tk_root = tkinter.Tk()
    tk_root.withdraw()  # Hide the main window

    file_path = tkinter.filedialog.askdirectory(title="Select a Tinymate clips directory")
    if file_path:
        sys.stdout.write(repr(file_path.encode()))

if len(sys.argv)>1 and sys.argv[1] == 'dir-path-dialog':
    dir_path_dialog()
    exit()

# we spawn subprocesses to export the movie to GIF, MP4 and a PNG sequence
# every time we close a movie (if we reopen it, we interrupt the exporting
# process and then restart upon closing; when we exit the application,
# we wait for all the exporting to finish so the user knows all the exported
# data is ready upon exiting)

import collections
import signal
import uuid
import json

IWIDTH = 1920
IHEIGHT = 1080
FRAME_RATE = 12
CLIP_FILE = 'movie.json' # on Windows, this starting with 'm' while frame0000.png starts with 'f'
# makes the png the image inside the directory icon displayed in Explorer... which is nice
FRAME_FMT = 'frame%04d.png'
CURRENT_FRAME_FILE = 'current_frame.png'
BACKGROUND = (240, 235, 220)
PEN = (20, 20, 20)

class CachedItem:
    def compute_key(self):
        '''a key is a tuple of:
        1. a list of tuples mapping IDs to versions. a cached item referencing
        unknown IDs or IDs with old versions is eventually garbage-collected
        2. any additional info making the key unique.
        
        compute_key returns the key computed from the current system state.
        for example, CachedThumbnail(pos=5) might return a dictionary mapping
        the IDs of frames making up frame 5 in every layer, and the string
        "thumbnail." The number 5 is not a part of the key; if frame 6
        is made of the same frames in every layer, CachedThumbnail(pos=6)
        will compute the same key. If in the future a CachedThumbnail(pos=5)
        is created computing a different key because the movie was edited,
        you'll get a cache miss as expected.
        '''
        return {}, None

    def compute_value(self):
        '''returns the value - used upon cached miss. note that CachedItems
        are not kept in the cache themselves - only the keys and the values.'''
        return None

# there are 2 reasons to evict a cached item:
# * no more room in the cache - evict the least recently used items until there's room
# * the cached item has no chance to be useful - eg it was computed from a deleted or
#   since-edited frame - this is done by collect_garbage() and assisted by update_id()
#   and delete_id()
class Cache:
    class Miss:
        pass
    MISS = Miss()
    def __init__(self):
        self.key2value = collections.OrderedDict()
        self.id2version = {}
        self.debug = False
        self.gc_iter = 0
        self.last_check = {}
        # these are per-gc iteration counters
        self.computed_bytes = 0
        self.cached_bytes = 0
        # sum([self.size(value) for value in self.key2value.values()])
        self.cache_size = 0
    def size(self,value):
        try:
            # surface
            return value.get_width() * value.get_height() * 4
        except:
            try:
                # numpy array
                return reduce(lambda x,y: x*y, value.shape)
            except:
                return 0
    def fetch(self, cached_item):
        key = (cached_item.compute_key(), (IWIDTH, IHEIGHT))
        value = self.key2value.get(key, Cache.MISS)
        if value is Cache.MISS:
            value = cached_item.compute_value()
            vsize = self.size(value)
            self.computed_bytes += vsize
            self.cache_size += vsize
            self._evict_lru_as_needed()
            self.key2value[key] = value
        else:
            self.key2value.move_to_end(key)
            self.cached_bytes += self.size(value)
            if self.debug and self.last_check.get(key, 0) < self.gc_iter:
                # slow debug mode
                ref = cached_item.compute_value()
                if not np.array_equal(pg.surfarray.pixels3d(ref), pg.surfarray.pixels3d(value)) or not np.array_equal(pg.surfarray.pixels_alpha(ref), pg.surfarray.pixels_alpha(value)):
                    print('HIT BUG!',key)
                self.last_check[key] = self.gc_iter
        return value

    def _evict_lru_as_needed(self):
        while self.cache_size > MAX_CACHE_BYTE_SIZE or len(self.key2value) > MAX_CACHED_ITEMS:
            key, value = self.key2value.popitem(last=False)
            self.cache_size -= self.size(value)

    def update_id(self, id, version):
        self.id2version[id] = version
    def delete_id(self, id):
        if id in self.id2version:
            del self.id2version[id]
    def stale(self, key):
        id2version, _ = key[0]
        for id, version in id2version:
            current_version = self.id2version.get(id)
            if current_version is None or version < current_version:
                #print('stale',id,version,current_version)
                return True
        return False
    def collect_garbage(self):
        orig = len(self.key2value)
        orig_size = self.cache_size
        for key, value in list(self.key2value.items()):
            if self.stale(key):
                del self.key2value[key]
                self.cache_size -= self.size(value)
        #print('gc',orig,orig_size,'->',len(self.key2value),self.cache_size,'computed',self.computed_bytes,'cached',self.cached_bytes,tdiff())
        self.gc_iter += 1
        self.computed_bytes = 0
        self.cached_bytes = 0

cache = Cache()

def fit_to_resolution(surface):
    w,h = surface.get_width(), surface.get_height()
    if w == IWIDTH and h == IHEIGHT:
        return surface
    elif w == IHEIGHT and h == IWIDTH:
        return pg.transform.rotate(surface, 90 * (1 if w>h else -1)) 
    else:
        return pg.transform.scale(surface, (w, h))

def new_frame():
    frame = pygame.Surface((IWIDTH, IHEIGHT), pygame.SRCALPHA)
    frame.fill(BACKGROUND)
    pg.surfarray.pixels_alpha(frame)[:] = 0
    return frame

class Frame:
    def __init__(self, dir, layer_id=None, frame_id=None, read_pixels=True):
        self.dir = dir
        self.layer_id = layer_id
        if frame_id is not None: # id - load the surfaces from the directory
            self.id = frame_id
            self.del_pixels()
            if read_pixels:
                self.read_pixels()
        else:
            self.id = str(uuid.uuid1())
            self.color = None
            self.lines = None

        # we don't aim to maintain a "perfect" dirty flag such as "doing 5 things and undoing
        # them should result in dirty==False." The goal is to avoid gratuitous saving when
        # scrolling thru the timeline, which slows things down and prevents reopening
        # clips at the last actually-edited frame after exiting the program
        self.dirty = False
        # similarly to dirty, version isn't a perfect version number; we're fine with it
        # going up instead of back down upon undo, or going up by more than 1 upon a single
        # editing operation. the version number is used for knowing when a cache hit
        # would produce stale data; if we occasionally evict valid data it's not as bad
        # as for hits to occasionally return stale data
        self.version = 0
        self.hold = False

        cache.update_id(self.cache_id(), self.version)

        self.compression_subprocess = None

    def __del__(self):
        cache.delete_id(self.cache_id())

    def read_pixels(self):
        for surf_id in self.surf_ids():
            for fname in self.filenames_png_bmp(surf_id):
                if os.path.exists(fname):
                    setattr(self,surf_id,fit_to_resolution(pygame.image.load(fname)))
                    break

    def del_pixels(self):
        for surf_id in self.surf_ids():
            setattr(self,surf_id,None)

    def empty(self): return self.color is None

    def _create_surfaces_if_needed(self):
        if not self.empty():
            return
        self.color = new_frame()
        self.lines = pg.Surface((self.color.get_width(), self.color.get_height()), pygame.SRCALPHA)
        self.lines.fill(PEN)
        pygame.surfarray.pixels_alpha(self.lines)[:] = 0

    def get_content(self): return self.color.copy(), self.lines.copy()
    def set_content(self, content):
        color, lines = content
        self.color = fit_to_resolution(color.copy())
        self.lines = fit_to_resolution(lines.copy())
    def clear(self):
        self.color = None
        self.lines = None

    def increment_version(self):
        self._create_surfaces_if_needed()
        self.dirty = True
        self.version += 1
        cache.update_id(self.cache_id(), self.version)

    def surf_ids(self): return ['lines','color']
    def get_width(self): return IWIDTH
    def get_height(self): return IHEIGHT
    def get_rect(self): return empty_frame().color.get_rect()

    def surf_by_id(self, surface_id):
        s = getattr(self, surface_id)
        return s if s is not None else empty_frame().surf_by_id(surface_id)

    def surface(self):
        if self.empty():
            return empty_frame().color
        s = self.color.copy()
        s.blit(self.lines, (0, 0))
        return s

    def filenames_png_bmp(self,surface_id):
        fname = f'{self.id}-{surface_id}.'
        if self.layer_id:
            fname = os.path.join(f'layer-{self.layer_id}', fname)
        fname = os.path.join(self.dir, fname)
        return fname+'png', fname+'bmp'
    def wait_for_compression_to_finish(self):
        if self.compression_subprocess:
            self.compression_subprocess.wait()
        self.compression_subprocess = None
    def save(self):
        if self.dirty:
            self.wait_for_compression_to_finish()
            fnames = []
            for surf_id in self.surf_ids():
                fname_png, fname_bmp = self.filenames_png_bmp(surf_id)
                pygame.image.save(self.surf_by_id(surf_id), fname_bmp)
                fnames += [fname_bmp, fname_png]
            self.compression_subprocess = subprocess.Popen([sys.executable, sys.argv[0], 'compress-and-remove']+fnames)
            self.dirty = False
    def delete(self):
        self.wait_for_compression_to_finish()
        for surf_id in self.surf_ids():
            for fname in self.filenames_png_bmp(surf_id):
                if os.path.exists(fname):
                    os.unlink(fname)

    def size(self):
        # a frame is 2 RGBA surfaces
        return (self.get_width() * self.get_height() * 8) if not self.empty() else 0

    def cache_id(self): return (self.id, self.layer_id) if not self.empty() else None
    def cache_id_version(self): return self.cache_id(), self.version

    def fit_to_resolution(self):
        if self.empty():
            return
        for surf_id in self.surf_ids():
            setattr(self, surf_id, fit_to_resolution(self.surf_by_id(surf_id)))

_empty_frame = Frame('')
def empty_frame():
    global _empty_frame
    if not _empty_frame.empty() and (_empty_frame.color.get_width() != IWIDTH or _empty_frame.color.get_height() != IHEIGHT):
        _empty_frame = Frame('')
    _empty_frame._create_surfaces_if_needed()
    return _empty_frame

class Layer:
    def __init__(self, frames, dir, layer_id=None):
        self.dir = dir
        self.frames = frames
        self.id = layer_id if layer_id else str(uuid.uuid1())
        self.lit = True
        self.visible = True
        self.locked = False
        for frame in frames:
            frame.layer_id = self.id
        subdir = self.subdir()
        if not os.path.isdir(subdir):
            os.makedirs(subdir)

    def surface_pos(self, pos):
        while self.frames[pos].hold:
            pos -= 1
        return pos

    def frame(self, pos): # return the closest frame in the past where hold is false
        return self.frames[self.surface_pos(pos)]

    def subdir(self): return os.path.join(self.dir, f'layer-{self.id}')
    def deleted_subdir(self): return self.subdir() + '-deleted'

    def delete(self):
        for frame in self.frames:
            frame.wait_for_compression_to_finish()
        os.rename(self.subdir(), self.deleted_subdir())
    def undelete(self): os.rename(self.deleted_subdir(), self.subdir())

    def toggle_locked(self): self.locked = not self.locked
    def toggle_lit(self): self.lit = not self.lit
    def toggle_visible(self):
        self.visible = not self.visible
        self.lit = self.visible

def default_progress_callback(done_items, total_items): pass

class MovieData:
    def __init__(self, dir, read_pixels=True, progress=default_progress_callback):
        self.dir = dir
        if not os.path.isdir(dir): # new clip
            os.makedirs(dir)
            self.frames = [Frame(self.dir)]
            self.pos = 0
            self.layers = [Layer(self.frames, dir)]
            self.layer_pos = 0
            self.frames[0].save()
            self.save_meta()
        else:
            with open(os.path.join(dir, CLIP_FILE), 'r') as clip_file:
                clip = json.loads(clip_file.read())
            global IWIDTH, IHEIGHT

            movie_width, movie_height = clip.get('resolution',(IWIDTH,IHEIGHT))
            frame_ids = clip['frame_order']
            layer_ids = clip['layer_order']
            holds = clip['hold']
            visible = clip.get('layer_visible', [True]*len(layer_ids))
            locked = clip.get('layer_locked', [False]*len(layer_ids))

            IWIDTH, IHEIGHT = movie_width, movie_height

            done = 0
            total = len(layer_ids) * len(frame_ids)

            self.layers = []
            for layer_index, layer_id in enumerate(layer_ids):
                frames = []
                for frame_index, frame_id in enumerate(frame_ids):
                    frame = Frame(dir, layer_id, frame_id, read_pixels=read_pixels)
                    frame.hold = holds[layer_index][frame_index]
                    frames.append(frame)

                    done += 1
                    progress(done, total)

                layer = Layer(frames, dir, layer_id)
                layer.visible = visible[layer_index]
                layer.locked = locked[layer_index]
                self.layers.append(layer)

            self.pos = clip['frame_pos']
            self.layer_pos = clip['layer_pos']
            self.frames = self.layers[self.layer_pos].frames

    def save_meta(self):
        # TODO: save light table settings
        clip = {
            'resolution':[IWIDTH, IHEIGHT],
            'frame_pos':self.pos,
            'layer_pos':self.layer_pos,
            'frame_order':[frame.id for frame in self.frames],
            'layer_order':[layer.id for layer in self.layers],
            'layer_visible':[layer.visible for layer in self.layers],
            'layer_locked':[layer.locked for layer in self.layers],
            'hold':[[frame.hold for frame in layer.frames] for layer in self.layers],
        }
        fname = os.path.join(self.dir, CLIP_FILE)
        text = json.dumps(clip,indent=2)
        try:
            with open(fname) as clip_file:
                if text == clip_file.read():
                    return # no changes
        except FileNotFoundError:
            pass
        self.edited_since_export = True
        with open(fname, 'w') as clip_file:
            clip_file.write(text)

    def gif_path(self): return os.path.realpath(self.dir)+'-GIF.gif'
    def mp4_path(self): return os.path.realpath(self.dir)+'-MP4.mp4'
    def png_path(self, i): return os.path.join(self.dir, FRAME_FMT%i)

    def _blit_layers(self, layers, pos, transparent=False, include_invisible=False, width=None, height=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT
        s = pygame.Surface((width, height), pygame.SRCALPHA)
        if not transparent:
            s.fill(BACKGROUND)
        surfaces = []
        for layer in layers:
            if not layer.visible and not include_invisible:
                continue
            if width==IWIDTH and height==IHEIGHT:
                f = layer.frame(pos)
                surfaces.append(f.surf_by_id('color'))
                surfaces.append(f.surf_by_id('lines'))
            else:
                surfaces.append(movie.get_thumbnail(pos, width, height, transparent_single_layer=self.layers.index(layer)))
        s.blits([(surface, (0, 0), (0, 0, width, height)) for surface in surfaces])
        return s


# a supposed advantage of this verbose method of writing MP4s using PyUV over "just" using imageio
# is that `pip install av` installs ffmpeg libraries so you don't need to worry
# separately about installing ffmpeg. imageio also fails in a "TiffWriter" at the
# time of writing, or if the "fps" parameter is removed, creates a giant .mp4
# output file that nothing seems to be able to play.
class MP4:
    def __init__(self, fname, width, height, fps):
        self.output = av.open(fname, 'w', format='mp4')
        self.stream = self.output.add_stream('h264', str(fps))
        self.stream.width = width
        self.stream.height = height
        self.stream.pix_fmt = 'yuv420p' # Windows Media Player eats this up unlike yuv444p
        self.stream.options = {'crf': '17'} # quite bad quality with smaller file sizes without this
    def write_frame(self, pixels):
        frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
        packet = self.stream.encode(frame)
        self.output.mux(packet)
    def close(self):
        packet = self.stream.encode(None)
        self.output.mux(packet)
        self.output.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()

interrupted = False
def signal_handler(signum, stack):
    global interrupted
    interrupted = True

def check_if_interrupted():
    if interrupted:
        #print('interrupted',sys.argv[2])
        raise KeyboardInterrupt

def transpose_xy(image):
    return np.transpose(image, [1,0,2]) if len(image.shape)==3 else np.transpose(image, [1,0])

def export(clipdir):
    #print('exporting',clipdir)
    movie = MovieData(clipdir, read_pixels=False)
    check_if_interrupted()

    assert FRAME_RATE==12
    with imageio.get_writer(movie.gif_path(), fps=FRAME_RATE, loop=0) as gif_writer:
        with MP4(movie.mp4_path(), IWIDTH, IHEIGHT, fps=24) as mp4_writer:
            for i in range(len(movie.frames)):
                # TODO: render the PNGs transparently to the exported sequence
                for layer in movie.layers:
                    layer.frame(i).read_pixels()

                frame = movie._blit_layers(movie.layers, i)
                check_if_interrupted()
                pixels = transpose_xy(pygame.surfarray.pixels3d(frame))
                check_if_interrupted()
                gif_writer.append_data(pixels)
                # append each frame twice at MP4 to get a standard 24 fps frame rate
                # (for GIFs there's less likelihood that something has a problem with
                # "non-standard 12 fps" (?))
                check_if_interrupted() 
                mp4_writer.write_frame(pixels)
                check_if_interrupted() 
                mp4_writer.write_frame(pixels)
                check_if_interrupted() 
                imageio.imwrite(movie.png_path(i), pixels)
                check_if_interrupted()

                for layer in movie.layers:
                    layer.frame(i).del_pixels() # save memory footprint - we might have several background export processes
                check_if_interrupted()

    #print('done with',clipdir)

if len(sys.argv)>1 and sys.argv[1] == 'export':
    signal.signal(signal.SIGBREAK if on_windows else signal.SIGINT, signal_handler)

    try:
        import pygame
        pg = pygame
        _empty_frame = Frame('')
        check_if_interrupted()

        import av
        check_if_interrupted()

        export(sys.argv[2])
    except KeyboardInterrupt:
        pass # not an error
    except:
        import traceback
        traceback.print_exc()
    finally:
        exit()

def get_last_modified(filenames):
    f2mtime = {}
    for f in filenames:
        s = os.stat(f)
        f2mtime[f] = s.st_mtime
    return list(sorted(f2mtime.keys(), key=lambda f: f2mtime[f]))[-1]

def is_exported_png(f): return f.endswith('.png') and f != CURRENT_FRAME_FILE

class ExportProgressStatus:
    def __init__(self):
        self.first = True
    def _init(self, initial_clips):
        self.clip2frames = {}
        for clip in initial_clips:
            movie = MovieData(clip, read_pixels=False)
            self.clip2frames[clip] = len(movie.frames)
        self.total = sum(self.clip2frames.values())
        self.initial_clips = initial_clips
    def update(self, live_clips):
        self.done = 0
        import re
        fmt = re.compile(r'frame([0-9]+)\.png')
        for clip in live_clips:
            pngs = [f for f in os.listdir(clip) if is_exported_png(f)]
            if pngs:
                last = get_last_modified([os.path.join(clip, f) for f in pngs])
                m = fmt.match(os.path.basename(last))
                self.done += int(m.groups()[0]) + 1 # frame 3 being ready means 4 are done

        if self.first:
            self._init(live_clips)
            # don't show that we've already done most of the work if we have
            # a lot of progress to report the first time - always report progress
            # "from 0 to 100"
            self.total -= self.done
            self.done_before_we_started_looking = self.done
            self.done = 0
            self.first = False
        else:
            for clip in self.initial_clips: # clips we no longer need to check are still done
                if clip not in live_clips:
                    self.done += self.clip2frames[clip]
            # our "0%" included this amount of done stuff - don't go above 100%
            self.done -= self.done_before_we_started_looking

_empty_frame = Frame('')

import subprocess
import pygame.gfxdraw
if on_windows:
    import winpath
import math
import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import matplotlib
matplotlib.use('agg')  # turn off interactive backend
import io
import shutil
from scipy.interpolate import splprep

from skimage.morphology import flood_fill, binary_dilation, skeletonize
from scipy.ndimage import grey_dilation, grey_erosion, grey_opening, grey_closing
pg = pygame
pg.init()

#screen = pygame.display.set_mode((800, 350*2), pygame.RESIZABLE)
#screen = pygame.display.set_mode((350, 800), pygame.RESIZABLE)
#screen = pygame.display.set_mode((1200, 350), pygame.RESIZABLE)
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
screen.fill(BACKGROUND)
pygame.display.flip()
pygame.display.set_caption("Tinymate")

font = pygame.font.Font(size=screen.get_height()//15)

FADING_RATE = 3
MARGIN = (220, 215, 190)
UNDRAWABLE = (220-20, 215-20, 190-20)
SELECTED = (220-80, 215-80, 190-80)
UNUSED = SELECTED
PROGRESS = (192-45, 255-25, 192-45)
LAYERS_BELOW = (128,192,255)
LAYERS_ABOVE = (255,192,0)
WIDTH = 3 # the smallest width where you always have a pure pen color rendered along
# the line path, making our naive flood fill work well...
MEDIUM_ERASER_WIDTH = 5*WIDTH
BIG_ERASER_WIDTH = 20*WIDTH
CURSOR_SIZE = int(screen.get_width() * 0.07)
MAX_HISTORY_BYTE_SIZE = 1*1024**3
MAX_CACHE_BYTE_SIZE = 1*1024**3
MAX_CACHED_ITEMS = 2000

if on_windows:
    MY_DOCUMENTS = winpath.get_my_documents()
else:
    MY_DOCUMENTS = os.path.expanduser('~')

def set_wd(wd):
    global WD
    WD = wd
    if not os.path.exists(WD):
        os.makedirs(WD)
    
set_wd(os.path.join(MY_DOCUMENTS if MY_DOCUMENTS else '.', 'Tinymate'))
print('clips read from, and saved to',WD)

import time
# add tdiff() to printouts to see how many ms passed since the last call to tdiff()
prevts=time.time_ns()
def tdiff():
    global prevts
    now=time.time_ns()
    diff=(now-prevts)//10**6
    prevts = now
    return diff

class Timer:
    CALL_HISTORY = 30
    SCALE = 1/10**6
    def __init__(self,name):
        self.name = name
        self.total = 0
        self.calls = 0
        self.min = 2**60
        self.max = 0
        self.history = []
    def start(self):
        self.start_ns = time.time_ns()
    def stop(self):
        took = time.time_ns() - self.start_ns
        self.calls += 1
        self.total += took
        self.min = min(self.min, took)
        self.max = max(self.max, took)
        self.history.append(took)
        if len(self.history) > Timer.CALL_HISTORY:
            del self.history[0]
        return took * Timer.SCALE
    def show(self):
        scale = Timer.SCALE
        if self.calls>1:
            history = ' '.join([str(round(scale*h)) for h in self.history])
            return f'{self.name}: {round(scale*self.total/self.calls)} ms [{round(scale*self.min)}, {round(scale*self.max)}] in {self.calls} calls {history}'
        elif self.calls==1:
            return f'{self.name}: {round(scale*self.total)} ms'
        else:
            return f'{self.name}: never called'
    def __enter__(self):
        self.start()
    def __exit__(self, *args):
        self.stop()
        print(self.show())

class Timers:
    def __init__(self):
        self.timers = []
        self.timer2children = {}
    def add(self, name, indent=0):
        timer = Timer(name)
        timer.indent = indent
        self.timers.append(timer)
        return timer
    def show(self):
        for timer in self.timers:
            print(timer.indent*'  '+timer.show())

timers = Timers()
layout_draw_timer = timers.add('Layout.draw')
drawing_area_draw_timer = timers.add('DrawingArea.draw', indent=1)
timeline_area_draw_timer = timers.add('TimelineArea.draw', indent=1)
layers_area_draw_timer = timers.add('LayersArea.draw', indent=1)
movie_list_area_draw_timer = timers.add('MovieListArea.draw', indent=1)
pen_down_timer = timers.add('PenTool.on_mouse_down')
pen_move_timer = timers.add('PenTool.on_mouse_move')
pen_up_timer = timers.add('PenTool.on_mouse_up')
pen_draw_lines_timer = timers.add('drawLines', indent=1)
fit_curve_timer = timers.add('bspline_interp', indent=2)
pen_suggestions_timer = timers.add('pen suggestions', indent=1)
eraser_timer = timers.add('eraser',indent=1)
pen_fading_mask_timer = timers.add('fading_mask', indent=1)
paint_bucket_timer = timers.add('PaintBucketTool.on_mouse_down')
timeline_down_timer = timers.add('TimelineArea.on_mouse_down')
timeline_move_timer = timers.add('TimelineArea.on_mouse_move')

# interface with tinylib

def arr_base_ptr(arr): return arr.ctypes.data_as(ctypes.c_void_p)

def color_c_params(rgb):
    width, height, depth = rgb.shape
    assert depth == 3
    xstride, ystride, zstride = rgb.strides
    oft = 0
    bgr = 0
    if zstride == -1: # BGR image - walk back 2 bytes to get to the first blue pixel...
        # (many functions don't care about which channel is which and it's better to not
        # have to pass another stride argument to them)
        oft = -2
        zstride = 1
        bgr = 1
    assert xstride == 4 and zstride == 1, f'xstride={xstride}, ystride={ystride}, zstride={zstride}'
    ptr = ctypes.c_void_p(arr_base_ptr(rgb).value + oft)
    return ptr, ystride, width, height, bgr

def greyscale_c_params(grey, is_alpha=True):
    width, height = grey.shape
    xstride, ystride = grey.strides
    assert (xstride == 4 and is_alpha) or (xstride == 1 and not is_alpha), f'xstride={xstride} is_alpha={is_alpha}'
    ptr = arr_base_ptr(grey)
    return ptr, ystride, width, height

def make_color_int(rgba, is_bgr):
    r,g,b,a = rgba
    if is_bgr:
        return b | (g<<8) | (r<<16) | (a<<24)
    else:
        return r | (g<<8) | (b<<16) | (a<<24)

import numpy.ctypeslib as npct
import ctypes
tinylib = npct.load_library('tinylib','.')

# these are simple functions to test the assumptions regarding Surface numpy array layout
def meshgrid_color(rgb): tinylib.meshgrid_color(*color_c_params(rgb))
def meshgrid_alpha(alpha): tinylib.meshgrid_alpha(*greyscale_c_params(alpha))

fig = None
ax = None
fig_dict = {}
ax_dict = {}

def splev(x, tck):
    t, c, k = tck
    try:
        c[0][0]
        parametric = True
    except Exception:
        parametric = False
    if parametric:
        return list(map(lambda c, x=x, t=t, k=k: splev(x, [t, c, k]), c))

    x = np.asarray(x)
    xshape = x.shape
    x = x.ravel()
    y = np.zeros(x.shape, float)
    tinylib.splev(arr_base_ptr(t), t.shape[0], arr_base_ptr(c), k, arr_base_ptr(x), arr_base_ptr(y), y.shape[0])
    return y.reshape(xshape)

def should_make_closed(curve_length, bbox_length, endpoints_dist):
    if curve_length < bbox_length*0.85:
        # if the distance between the endpoints is <30% of the length of the curve, close it
        return endpoints_dist / curve_length < 0.3
    else: # "long and curvy" - only make closed when the endpoints are close relatively to the bbox length
        return endpoints_dist / bbox_length < 0.1

def bspline_interp(points, suggest_options, existing_lines):
    fit_curve_timer.start()
    x = np.array([1.*p[0] for p in points])
    y = np.array([1.*p[1] for p in points])

    okay = np.where(np.abs(np.diff(x)) + np.abs(np.diff(y)) > 0)
    x = np.r_[x[okay], x[-1]]#, x[0]]
    y = np.r_[y[okay], y[-1]]#, y[0]]

    def dist(i1, i2):
        return math.sqrt((x[i1]-x[i2])**2 + (y[i1]-y[i2])**2)
    curve_length = sum([dist(i, i+1) for i in range(len(x)-1)])

    results = []

    def add_result(tck, ufirst, ulast):
        step=(ulast-ufirst)/curve_length

        new_points = splev(np.arange(ufirst, ulast+step, step), tck)
        results.append(new_points)

    tck, u = splprep([x, y], s=len(x)/5)
    add_result(tck, u[0], u[-1])

    if not suggest_options:
        fit_curve_timer.stop()
        return results

    # check for intersections, throw out short segments between the endpoints and first/last intersection
    ix = np.round(results[0][0]).astype(int)
    iy = np.round(results[0][1]).astype(int)
    within_bounds = (ix >= 0) & (iy >= 0) & (ix < existing_lines.shape[0]) & (iy < existing_lines.shape[1])
    line_alphas = np.zeros(len(ix), int)
    line_alphas[within_bounds] = existing_lines[ix[within_bounds], iy[within_bounds]]
    intersections = np.where(line_alphas == 255)[0]

    def find_intersection_point(start, step):
        indexes = [intersections[start]]
        pos = start+step
        while pos < len(intersections) and pos >= 0 and abs(intersections[pos]-indexes[-1]) == 1:
            indexes.append(intersections[pos])
            pos += step
        return indexes[-1] #sum(indexes)/len(indexes)

    if len(intersections) > 0:
        len_first = intersections[0]
        len_last = len(ix) - intersections[-1]
        # look for clear alpha pixels along the path before the first and the last intersection - if we find some, we have >= 2 intersections
        two_or_more_intersections = len(np.where(line_alphas[intersections[0]:intersections[-1]] == 0)[0]) > 1

        first_short = two_or_more_intersections or len_first < len_last
        last_short = two_or_more_intersections or len_last <= len_first

        step=(u[-1]-u[0])/curve_length

        first_intersection = find_intersection_point(0, 1)
        last_intersection = find_intersection_point(len(intersections)-1, -1)
        uvals = np.arange(first_intersection if first_short else 0, (last_intersection if last_short else len(ix))+1, 1)*step
        new_points = splev(uvals, tck)
        return [new_points] + results

    # check if we'd like to attempt to close the line
    bbox_length = (np.max(x)-np.min(x))*2 + (np.max(y)-np.min(y))*2
    endpoints_dist = dist(0, -1)

    make_closed = len(points)>2 and should_make_closed(curve_length, bbox_length, endpoints_dist)

    if make_closed:
        tck, u = splprep([x, y], s=len(x)/5, per=True)
        add_result(tck, u[0], u[-1])
        return reversed(results)

    fit_curve_timer.stop()
    return results

def plotLines(points, ax, width, pwidth, suggest_options, existing_lines, image_width, image_height):
    results = []
    def add_results(px, py):
        minx = math.floor(max(0, np.min(px) - pwidth - 1))
        miny = math.floor(max(0, np.min(py) - pwidth - 1))
        maxx = math.ceil(min(image_height-1, np.max(px) + pwidth + 1))
        maxy = math.ceil(min(image_width-1, np.max(py) + pwidth + 1))

        line, = ax.plot(py,px, linestyle='solid', color='k', linewidth=width, scalex=False, scaley=False, solid_capstyle='round', aa=True)

        canvas = FigureCanvas(fig)
        fig.draw_artist(line)
        canvas.flush_events()

        pixel_data = canvas.buffer_rgba()

        # TODO: it would be nice to slice before converting to numpy array rather than first
        # convert and then slice which costs ~3 ms... but doing that gives a "memoryview: invalid slice key."
        # perhaps canvas.copy_from_bbox() instead of buffer_rgba() could be helpful?..
        results.append(((np.array(pixel_data)[minx:maxx+1,miny:maxy+1,3]), (minx, miny, maxx, maxy)))

    if len(set(points)) == 1:
        x,y = points[0]
        eps = 0.001
        points = [(x+eps, y+eps)] + points
    try:
        for path in bspline_interp(points, suggest_options, existing_lines):
            px, py = path[0], path[1]
            add_results(px, py)
    except:
        px = np.array([x for x,y in points])
        py = np.array([y for x,y in points])
        add_results(px, py)

    return results

def drawLines(image_height, image_width, points, width, suggest_options, existing_lines):
    global fig_dict
    global ax_dict
    global fig
    global ax
    res = (image_width, image_height)
    if res not in fig_dict:
        fig, ax = plt.subplots()
        ax.axis('off')
        fig.set_size_inches(image_width/fig.get_dpi(), image_height/fig.get_dpi())
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        fig_dict[res] = fig
        ax_dict[res] = ax
    
        def plot_reset():
            plt.cla()
            plt.xlim(0, image_width)
            plt.ylim(0, image_height)
            ax.invert_yaxis()
            ax.spines[['left', 'right', 'bottom', 'top']].set_visible(False)
            ax.tick_params(left=False, right=False, bottom=False, top=False)

        plot_reset()
    else:
        fig = fig_dict[res]
        ax = ax_dict[res]

    pwidth = width
    width *= 72 / fig.get_dpi()

    return plotLines(points, ax, width, pwidth, suggest_options, existing_lines, image_width, image_height)

def drawCircle( screen, x, y, color, width):
    pygame.draw.circle( screen, color, ( x, y ), width/2 )

def drawLine(screen, pos1, pos2, color, width):
    pygame.draw.line(screen, color, pos1, pos2, width)

def make_surface(width, height):
    return pg.Surface((width, height), screen.get_flags(), screen.get_bitsize(), screen.get_masks())

def scale_image(surface, width=None, height=None):
    now = time.time_ns()
    assert width or height
    if not height:
        height = int(surface.get_height() * width / surface.get_width())
    if not width:
        width = int(surface.get_width() * height / surface.get_height())
    ret = pg.transform.smoothscale(surface, (width, height))
    elapsed = time.time_ns() - now
    #print('scale_image',f'{surface.get_width()}x{surface.get_height()} -> {width}x{height} ({elapsed/10**6} ms)')
    return ret

def minmax(v, minv, maxv):
    return min(maxv,max(minv,v))

def load_cursor(file, flip=False, size=CURSOR_SIZE, hot_spot=(0,1), min_alpha=192, edit=lambda x: x, hot_spot_offset=(0,0)):
  surface = pg.image.load(file)
  surface = scale_image(surface, size, size*surface.get_height()/surface.get_width())#pg.transform.scale(surface, (CURSOR_SIZE, CURSOR_SIZE))
  if flip:
      surface = pg.transform.flip(surface, True, True)
  non_transparent_surface = surface.copy()
  alpha = pg.surfarray.pixels_alpha(surface)
  alpha[:] = np.minimum(alpha, min_alpha)
  del alpha
  surface = edit(surface)
  hotx = minmax(int(hot_spot[0] * surface.get_width()) + hot_spot_offset[0], 0, surface.get_width()-1)
  hoty = minmax(int(hot_spot[1] * surface.get_height()) + hot_spot_offset[1], 0, surface.get_height()-1)
  return pg.cursors.Cursor((hotx, hoty), surface), non_transparent_surface

def add_circle(image, radius, color=(255,0,0,128), outline_color=(0,0,0,128)):
    new_width = radius + image.get_width()
    new_height = radius + image.get_height()
    result = pg.Surface((new_width, new_height), pg.SRCALPHA)
    pg.gfxdraw.filled_circle(result, radius, new_height-radius, radius, outline_color)
    pg.gfxdraw.filled_circle(result, radius, new_height-radius, radius-WIDTH+1, (0,0,0,0))
    pg.gfxdraw.filled_circle(result, radius, new_height-radius, radius-WIDTH+1, color)
    result.blit(image, (radius, 0))
    return result

pencil_cursor = load_cursor('pen.png')
pencil_cursor = (pencil_cursor[0], pg.image.load('pen-tool.png'))
eraser_cursor = load_cursor('eraser.png')
eraser_cursor = (eraser_cursor[0], pg.image.load('eraser-tool.png'))
eraser_medium_cursor = load_cursor('eraser.png', size=int(CURSOR_SIZE*1.5), edit=lambda s: add_circle(s, MEDIUM_ERASER_WIDTH//2), hot_spot_offset=(MEDIUM_ERASER_WIDTH//2,-MEDIUM_ERASER_WIDTH//2))
eraser_medium_cursor = (eraser_medium_cursor[0], eraser_cursor[1])
eraser_big_cursor = load_cursor('eraser.png', size=int(CURSOR_SIZE*2), edit=lambda s: add_circle(s, BIG_ERASER_WIDTH//2), hot_spot_offset=(BIG_ERASER_WIDTH//2,-BIG_ERASER_WIDTH//2))
eraser_big_cursor = (eraser_big_cursor[0], eraser_cursor[1])
flashlight_cursor = load_cursor('flashlight.png')
flashlight_cursor = (flashlight_cursor[0], pg.image.load('flashlight-tool.png')) 
paint_bucket_cursor = (load_cursor('paint_bucket.png')[1], pg.image.load('bucket-tool.png'))
blank_page_cursor = load_cursor('sheets.png', hot_spot=(0.5, 0.5))
garbage_bin_cursor = load_cursor('garbage.png', hot_spot=(0.5, 0.5))
# set_cursor can fail on some machines so we don't count on it to work.
# we set it early on to "give a sign of life" while the window is black;
# we reset it again before entering the event loop.
# if the cursors cannot be set the selected tool can still be inferred by
# the darker background of the tool selection button.
def try_set_cursor(c):
    try:
        pg.mouse.set_cursor(c)
    except:
        pass
try_set_cursor(pencil_cursor[0])

def bounding_rectangle_of_a_boolean_mask(mask):
    # Sum along the vertical and horizontal axes
    vertical_sum = np.sum(mask, axis=1)
    if not np.any(vertical_sum):
        return None
    horizontal_sum = np.sum(mask, axis=0)

    minx, maxx = np.where(vertical_sum)[0][[0, -1]]
    miny, maxy = np.where(horizontal_sum)[0][[0, -1]]

    return minx, maxx, miny, maxy

class HistoryItem:
    def __init__(self, surface_id, bbox=None):
        self.surface_id = surface_id
        if not bbox:
            surface = self.curr_surface().copy()
            self.minx = 10**9
            self.miny = 10**9
            self.maxx = -10**9
            self.maxy = -10**9
        else:
            surface = self.curr_surface()        
            self.minx, self.miny, self.maxx, self.maxy = bbox

        self.saved_alpha = pg.surfarray.pixels_alpha(surface)
        self.saved_rgb = pg.surfarray.pixels3d(surface) if surface_id == 'color' else None
        self.pos = movie.pos
        self.layer_pos = movie.layer_pos
        self.optimized = False

        if bbox:
            self.saved_alpha = self.saved_alpha[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
            if self.saved_rgb is not None:
                self.saved_rgb = self.saved_rgb[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
            self.optimized = True

    def curr_surface(self):
        return movie.edit_curr_frame().surf_by_id(self.surface_id)
    def nop(self):
        return self.saved_alpha is None
    def undo(self):
        if self.nop():
            return

        if self.pos != movie.pos or self.layer_pos != movie.layer_pos:
            print(f'WARNING: HistoryItem at the wrong position! should be {self.pos} [layer {self.layer_pos}], but is {movie.pos} [layer {movie.layer_pos}]')
        movie.seek_frame_and_layer(self.pos, self.layer_pos) # we should already be here, but just in case

        # we could have created this item a bit more quickly with a bit more code but doesn't seem worth it
        redo = HistoryItem(self.surface_id)

        frame = self.curr_surface()
        if self.optimized:
            pg.surfarray.pixels_alpha(frame)[self.minx:self.maxx+1, self.miny:self.maxy+1] = self.saved_alpha
            if self.saved_rgb is not None:
                pg.surfarray.pixels3d(frame)[self.minx:self.maxx+1, self.miny:self.maxy+1] = self.saved_rgb
        else:
            pg.surfarray.pixels_alpha(frame)[:] = self.saved_alpha
            if self.saved_rgb is not None:
                pg.surfarray.pixels3d(frame)[:] = self.saved_rgb

        redo.optimize()
        return redo
    def optimize(self):
        if self.optimized:
            return

        mask = self.saved_alpha != pg.surfarray.pixels_alpha(self.curr_surface())
        if self.saved_rgb is not None:
            mask |= np.any(self.saved_rgb != pg.surfarray.pixels3d(self.curr_surface()), axis=2)
        brect = bounding_rectangle_of_a_boolean_mask(mask)

        if brect is None: # this can happen eg when drawing lines on an already-filled-with-lines area
            self.saved_alpha = None
            self.saved_rgb = None
            return
        
        self.minx, self.maxx, self.miny, self.maxy = brect
        self.saved_alpha = self.saved_alpha[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
        if self.saved_rgb is not None:
            self.saved_rgb = self.saved_rgb[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
        self.optimized = True

    def __str__(self):
        return f'HistoryItem(pos={self.pos}, rect=({self.minx}, {self.miny}, {self.maxx}, {self.maxy}))'

    def byte_size(self):
        if self.nop():
            return 0
        return self.saved_alpha.nbytes + (self.saved_rgb.nbytes if self.saved_rgb is not None else 0)

class HistoryItemSet:
    def __init__(self, items):
        self.items = [item for item in items if item is not None]
    def nop(self):
        for item in self.items:
            if not item.nop():
                return False
        return True
    def undo(self):
        return HistoryItemSet(list(reversed([item.undo() for item in self.items])))
    def optimize(self):
        for item in self.items:
            item.optimize()
        self.items = [item for item in self.items if not item.nop()]
    def byte_size(self):
        return sum([item.byte_size() for item in self.items])

def scale_and_preserve_aspect_ratio(w, h, width, height):
    if width/height > w/h:
        scaled_width = w*height/h
        scaled_height = h*scaled_width/w
    else:
        scaled_height = h*width/w
        scaled_width = w*scaled_height/h
    return scaled_width, scaled_height

class Button:
    def __init__(self):
        self.button_surface = None
    def draw(self, rect, cursor_surface):
        left, bottom, width, height = rect
        _, _, w, h = cursor_surface.get_rect()
        scaled_width, scaled_height = scale_and_preserve_aspect_ratio(w, h, width, height)
        if not self.button_surface:
            surface = scale_image(cursor_surface, scaled_width, scaled_height)
            self.button_surface = surface
        screen.blit(self.button_surface, (left+(width-scaled_width)/2, bottom+height-scaled_height))

locked_image = pg.image.load('locked.png')
invisible_image = pg.image.load('eye_shut.png')
def curr_layer_locked():
    effectively_locked = movie.curr_layer().locked or not movie.curr_layer().visible
    if effectively_locked: # invisible layers are effectively locked but we show it differently
        reason_image = locked_image if movie.curr_layer().locked else invisible_image
        fading_mask = new_frame()
        fading_mask.blit(reason_image, ((fading_mask.get_width()-reason_image.get_width())//2, (fading_mask.get_height()-reason_image.get_height())//2))
        fading_mask.set_alpha(192)
        layout.drawing_area().set_fading_mask(fading_mask)
        layout.drawing_area().fade_per_frame = 192/(FADING_RATE*3)
    return effectively_locked

class PenTool(Button):
    def __init__(self, eraser=False, width=WIDTH):
        Button.__init__(self)
        self.prev_drawn = None
        self.color = BACKGROUND if eraser else PEN
        self.eraser = eraser
        self.width = width
        self.circle_width = (width//2)*2
        self.points = []
        self.lines_array = None
        self.suggestion_mask = None
        if self.eraser:
            self.alpha_surface = None

    def on_mouse_down(self, x, y):
        if curr_layer_locked():
            return
        pen_down_timer.start()
        self.points = []
        self.bucket_color = None
        self.lines_array = pg.surfarray.pixels_alpha(movie.edit_curr_frame().surf_by_id('lines'))
        if self.eraser:
            if not self.alpha_surface:
                self.alpha_surface = new_frame()
            pg.surfarray.pixels_red(self.alpha_surface)[:] = 0
        self.on_mouse_move(x,y)
        pen_down_timer.stop()

    def on_mouse_up(self, x, y):
        if curr_layer_locked():
            return
        pen_up_timer.start()
        self.lines_array = None
        drawing_area = layout.drawing_area()
        cx, cy = drawing_area.xy2frame(x, y)
        self.points.append((cx,cy))
        self.prev_drawn = None
        frame = movie.edit_curr_frame().surf_by_id('lines')
        lines = pygame.surfarray.pixels_alpha(frame)

        line_width = self.width * (1 if self.width == WIDTH else drawing_area.xscale)
        pen_draw_lines_timer.start()
        line_options = drawLines(frame.get_width(), frame.get_height(), self.points, line_width, suggest_options=not self.eraser, existing_lines=lines)
        pen_draw_lines_timer.stop()

        if self.eraser:
            eraser_timer.start()

            new_lines,(minx,miny,maxx,maxy) = line_options[0]
            bbox = (minx, miny, maxx, maxy)
            lines_history_item = HistoryItem('lines', bbox)
            lines[minx:maxx+1, miny:maxy+1] = np.minimum(255-new_lines, lines[minx:maxx+1, miny:maxy+1])

            def make_color_history_item(bbox):
                return HistoryItem('color', bbox)

            color = movie.edit_curr_frame().surf_by_id('color')
            color_rgb = pg.surfarray.pixels3d(color)
            color_alpha = pg.surfarray.pixels_alpha(color)
            bucket_color = self.bucket_color if self.bucket_color else BACKGROUND+(0,) 
            color_history_item = flood_fill_color_based_on_lines(color_rgb, color_alpha, lines, round(cx), round(cy), bucket_color, make_color_history_item)
            history_item = HistoryItemSet([lines_history_item, color_history_item])

            history.append_item(history_item)

            eraser_timer.stop()
        else:
            pen_suggestions_timer.start()

            prev_history_item = None
            items = []

            for new_lines,(minx,miny,maxx,maxy) in line_options:
                bbox = (minx, miny, maxx, maxy)
                if prev_history_item:
                    bbox = (min(prev_history_item.minx,minx), min(prev_history_item.miny,miny), max(prev_history_item.maxx,maxx), max(prev_history_item.maxy,maxy))
                history_item = HistoryItem('lines', bbox)
                if prev_history_item:
                    prev_history_item.undo()

                lines[minx:maxx+1, miny:maxy+1] = np.maximum(new_lines, lines[minx:maxx+1, miny:maxy+1])

                items.append(history_item)
                if not prev_history_item:
                    prev_history_item = history_item

            history.append_suggestions(items)

            pen_suggestions_timer.stop()
        
        if len(line_options)>1:
            pen_fading_mask_timer.start()
            if self.suggestion_mask is None or (self.suggestion_mask.get_width() != IWIDTH or self.suggestion_mask.get_height() != IHEIGHT):
                self.suggestion_mask = pg.Surface((IWIDTH, IHEIGHT), pg.SRCALPHA)
                self.suggestion_mask.fill((0,255,0))
            alt_option, (minx,miny,maxx,maxy) = line_options[-2]
            alpha = pg.surfarray.pixels_alpha(self.suggestion_mask)
            alpha[:] = 0
            alpha[minx:maxx+1,miny:maxy+1] = alt_option
            self.suggestion_mask.set_alpha(10)
            drawing_area.set_fading_mask(self.suggestion_mask)
            class Fading:
                def __init__(self):
                    self.i = 0
                def fade(self, alpha, _):
                    self.i += 1
                    if self.i == 1:
                        return 10
                    if self.i == 2:
                        return 130
                    else:
                        return 110-self.i*10
            drawing_area.fading_func = Fading().fade
            pen_fading_mask_timer.stop()

        pen_up_timer.stop()

    def on_mouse_move(self, x, y):
        if curr_layer_locked():
            return
        pen_move_timer.start()
        drawing_area = layout.drawing_area()
        cx, cy = drawing_area.xy2frame(x, y)
        if self.eraser and self.bucket_color is None:
            nx, ny = round(cx), round(cy)
            if nx>=0 and ny>=0 and nx<self.lines_array.shape[0] and ny<self.lines_array.shape[1] and self.lines_array[nx,ny] == 0:
                self.bucket_color = movie.edit_curr_frame().surf_by_id('color').get_at((cx,cy))
        self.points.append((cx,cy))
        color = self.color if not self.eraser else (self.bucket_color if self.bucket_color else (255,255,255,0))
        expose_other_layers = self.eraser and color[3]==0
        if expose_other_layers:
            color = (255,0,0,0)
        draw_into = drawing_area.subsurface if not expose_other_layers else self.alpha_surface
        ox,oy = (0,0) if not expose_other_layers else (drawing_area.xmargin, drawing_area.ymargin)
        if self.prev_drawn:
            drawLine(draw_into, (self.prev_drawn[0]-ox, self.prev_drawn[1]-oy), (x-ox,y-oy), color, self.width)
        drawCircle(draw_into, x-ox, y-oy, color, self.circle_width)
        if expose_other_layers:
            alpha = pg.surfarray.pixels_red(draw_into)
            w, h = self.lines_array.shape
            def clipw(val): return max(0, min(val, w))
            def cliph(val): return max(0, min(val, h))
            px, py = self.prev_drawn if self.prev_drawn else (x, y)
            left = clipw(min(x-ox-self.width, px-ox-self.width))
            right = clipw(max(x-ox+self.width, px-ox+self.width))
            bottom = cliph(min(y-oy-self.width, py-oy-self.width))
            top = cliph(max(y-oy+self.width, py-oy+self.width))
            def render_surface(s):
                if not s:
                    return
                salpha = pg.surfarray.pixels_alpha(s)
                orig_alpha = salpha[left:right+1, bottom:top+1].copy()
                salpha[left:right+1, bottom:top+1] = np.minimum(orig_alpha, alpha[left:right+1,bottom:top+1])
                del salpha
                drawing_area.subsurface.blit(s, (ox+left,oy+bottom), (left,bottom,right-left+1,top-bottom+1))
                salpha = pg.surfarray.pixels_alpha(s)
                salpha[left:right+1, bottom:top+1] = orig_alpha

            render_surface(movie.curr_bottom_layers_surface(movie.pos, highlight=True, width=drawing_area.iwidth, height=drawing_area.iheight))
            render_surface(movie.curr_top_layers_surface(movie.pos, highlight=True, width=drawing_area.iwidth, height=drawing_area.iheight))
            render_surface(layout.timeline_area().combined_light_table_mask())

        self.prev_drawn = (x,y) 
        pen_move_timer.stop()

class NewDeleteTool(PenTool):
    def __init__(self, frame_func, clip_func, layer_func):
        PenTool.__init__(self)
        self.frame_func = frame_func
        self.clip_func = clip_func
        self.layer_func = layer_func

    def on_mouse_down(self, x, y): pass
    def on_mouse_up(self, x, y): pass
    def on_mouse_move(self, x, y): pass

# note that we don't actually use color_alpha, rather we assume that color_rgb is actually color_rgba...
# we assert that this is the case in color_c_params()
def flood_fill_color_based_on_lines(color_rgb, color_alpha, lines, x, y, bucket_color, bbox_callback=None):
    flood_code = 2
    global pen_mask
    pen_mask = lines==255

    rect = np.zeros(4, dtype=np.int32)
    region = arr_base_ptr(rect)
    mask_ptr, mask_stride, width, height = greyscale_c_params(pen_mask, is_alpha=False)
    tinylib.flood_fill_mask(mask_ptr, mask_stride, width, height, x, y, flood_code, region, 0)
    
    xstart, ystart, xlen, ylen = rect
    bbox_retval = (xstart, ystart, xstart+xlen-1, ystart+ylen-1)
    if bbox_callback:
        bbox_retval = bbox_callback(bbox_retval)

    color_ptr, color_stride, color_width, color_height, bgr = color_c_params(color_rgb)
    assert color_width == width and color_height == height
    new_color_value = make_color_int(bucket_color, bgr)
    tinylib.fill_color_based_on_mask(color_ptr, mask_ptr, color_stride, mask_stride, width, height, region, new_color_value, flood_code)

    del pen_mask
    pen_mask = None

    return bbox_retval

class PaintBucketTool(Button):
    def __init__(self,color):
        Button.__init__(self)
        self.color = color
    def on_mouse_down(self, x, y):
        paint_bucket_timer.start()
        self._on_mouse_down(x, y)
        paint_bucket_timer.stop()
    def _on_mouse_down(self, x, y):
        if curr_layer_locked():
            return
        x, y = layout.drawing_area().xy2frame(x,y)
        x, y = round(x), round(y)
        color_surface = movie.edit_curr_frame().surf_by_id('color')
        color_rgb = pg.surfarray.pixels3d(color_surface)
        color_alpha = pg.surfarray.pixels_alpha(color_surface)
        lines = pygame.surfarray.pixels_alpha(movie.edit_curr_frame().surf_by_id('lines'))

        if x < 0 or y < 0 or x >= lines.shape[0] or y >= lines.shape[1]:
            return
        
        if (np.array_equal(color_rgb[x,y,:], np.array(self.color[0:3])) and color_alpha[x,y] == self.color[3]) or lines[x,y] == 255:
            return # we never flood the lines themselves - they keep the PEN color in a separate layer;
            # and there's no point in flooding with the color the pixel already has

        def make_history_item(bbox):
            return HistoryItem('color', bbox)

        history_item = flood_fill_color_based_on_lines(color_rgb, color_alpha, lines, x, y, self.color, make_history_item)
        history.append_item(history_item)
        
    def on_mouse_up(self, x, y):
        pass
    def on_mouse_move(self, x, y):
        pass

NO_PATH_DIST = 10**6

from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

def skeleton_to_distances(skeleton, x, y):
    width, height = skeleton.shape
    yg, xg = np.meshgrid(np.arange(height), np.arange(width))
    dist = np.sqrt((xg - x)**2 + (yg - y)**2)

    # look at points around the point on the skeleton closest to the selected point
    # (and not around the selected point itself since nothing could be close enough to it)
    closest = np.argmin(dist[skeleton])
    x,y = xg[skeleton].flat[closest],yg[skeleton].flat[closest]

    dist = np.sqrt((xg - x)**2 + (yg - y)**2)
    skx, sky = np.where(skeleton & (dist < 200))
    if len(skx) == 0:
        return np.ones((width, height), int) * NO_PATH_DIST, NO_PATH_DIST
    closest = np.argmin((skx-x)**2 + (sky-y)**2)

    ixy = list(enumerate(zip(skx,sky)))
    xy2i = dict([((x,y),i) for i,(x,y) in ixy])

    data = [] 
    row_ind = []
    col_ind = []

    width, height = skeleton.shape
    neighbors = [(ox, oy) for ox in range(-1,2) for oy in range(-1,2) if ox or oy]
    for i,(x,y) in ixy:
        for ox, oy in neighbors:
            nx = ox+x
            ny = oy+y
            if nx >= 0 and ny >= 0 and nx < width and ny < height:
                j = xy2i.get((nx,ny), None)
                if j is not None:
                    data.append(1)
                    row_ind.append(i)
                    col_ind.append(xy2i[(nx,ny)])
    
    graph = csr_matrix((data, (row_ind, col_ind)), (len(ixy), len(ixy)))
    distance_matrix = dijkstra(graph, directed=False)

    distances = np.ones((width, height), int) * NO_PATH_DIST
    maxdist = 0
    for i,(x,y) in ixy:
        d = distance_matrix[closest,i]
        if not math.isinf(d):
            distances[x,y] = d
            maxdist = max(maxdist, d)

    return distances, maxdist

last_flood_mask = None
last_skeleton = None

import colorsys

def skeletonize_color_based_on_lines(color, lines, x, y):
    global last_flood_mask
    global last_skeleton

    pen_mask = lines == 255
    if pen_mask[x,y]:
        return

    flood_code = 2
    flood_mask = flood_fill(pen_mask.astype(np.byte), (x,y), flood_code) == flood_code
    if last_flood_mask is not None and np.array_equal(flood_mask, last_flood_mask):
        skeleton = last_skeleton
    else: 
        skeleton = skeletonize(flood_mask)

    fmb = binary_dilation(binary_dilation(skeleton))
    fading_mask = pg.Surface((flood_mask.shape[0], flood_mask.shape[1]), pg.SRCALPHA)

    fm = pg.surfarray.pixels3d(fading_mask)
    yg, xg = np.meshgrid(np.arange(flood_mask.shape[1]), np.arange(flood_mask.shape[0]))

    # Compute distance from each point to the specified center
    d, maxdist = skeleton_to_distances(skeleton, x, y)
    if maxdist != NO_PATH_DIST:
        d = (d == NO_PATH_DIST)*maxdist + (d != NO_PATH_DIST)*d # replace NO_PATH_DIST with maxdist
    else: # if all the pixels are far from clicked coordinate, make the mask bright instead of dim,
        # otherwise it might look like "the flashlight isn't working"
        #
        # note that this case shouldn't happen because we are highlighting points around the closest
        # point on the skeleton to the clocked coordinate and not around the clicked coordinate itself
        d = np.ones(lines.shape, int)
        maxdist = 10
    outer_d = -grey_dilation(-d, 3)
    inner = (255,255,255)
    outer = [255-ch for ch in color[x,y]]
    h,s,v = colorsys.rgb_to_hsv(*[o/255. for o in outer])
    s = 1
    v = 1
    outer = [255*o for o in colorsys.hsv_to_rgb(h,s,v)]
    for ch in range(3):
         fm[:,:,ch] = outer[ch]*(1-skeleton) + inner[ch]*skeleton
    pg.surfarray.pixels_alpha(fading_mask)[:] = fmb*255*np.maximum(0,(1- .90*outer_d/maxdist))

    return fading_mask

class FlashlightTool(Button):
    def __init__(self):
        Button.__init__(self)
    def on_mouse_down(self, x, y):
        x, y = layout.drawing_area().xy2frame(x,y)
        x, y = round(x), round(y)
        color = pygame.surfarray.pixels3d(movie.curr_frame().surf_by_id('color'))
        lines = pygame.surfarray.pixels_alpha(movie.curr_frame().surf_by_id('lines'))
        if x < 0 or y < 0 or x >= color.shape[0] or y >= color.shape[1]:
            return
        fading_mask = skeletonize_color_based_on_lines(color, lines, x, y)
        if not fading_mask:
            return
        fading_mask.set_alpha(255)
        layout.drawing_area().set_fading_mask(fading_mask)
        layout.drawing_area().fade_per_frame = 255/(FADING_RATE*15)
    def on_mouse_up(self, x, y): pass
    def on_mouse_move(self, x, y): pass

# layout:
#
# - some items can change the cursor [specifically the timeline], so need to know to restore it back to the
#   "current default cursor" when it was changed from it and the current mouse position is outside the
#   "special cursor area"
#
# - some items can change the current tool [specifically tool selection buttons], which changes the
#   current default cursor too 
#
# - the drawing area makes use of the current tool
#
# - the element sizes are relative to the screen size. [within its element area, the drawing area
#   and the timeline images use a 16:9 subset]

def scale_rect(rect):
    left, bottom, width, height = rect
    sw = screen.get_width()
    sh = screen.get_height()
    return (round(left*sw), round(bottom*sh), round(width*sw), round(height*sh))

class Layout:
    def __init__(self):
        self.elems = []
        _, _, self.width, self.height = screen.get_rect()
        self.is_pressed = False
        self.is_playing = False
        self.playing_index = 0
        self.tool = PenTool()
        self.full_tool = TOOLS['pencil']
        self.focus_elem = None

    def aspect_ratio(self): return self.width/self.height

    def add(self, rect, elem, draw_border=False):
        srect = scale_rect(rect)
        elem.rect = srect
        elem.subsurface = screen.subsurface(srect)
        elem.draw_border = draw_border
        getattr(elem, 'init', lambda: None)()
        self.elems.append(elem)

    def draw(self):
        if self.is_pressed:
            if self.focus_elem is self.drawing_area():
                return
            if not getattr(self.focus_elem,'redraw',True):
                return

        layout_draw_timer.start()

        screen.fill(MARGIN if self.is_playing else UNDRAWABLE)
        for elem in self.elems:
            if not self.is_playing or isinstance(elem, DrawingArea) or isinstance(elem, TogglePlaybackButton):
                elem.draw()
                if elem.draw_border:
                    pygame.draw.rect(screen, PEN, elem.rect, 1, 1)

        layout_draw_timer.stop()

    # note that pygame seems to miss mousemove events with a Wacom pen when it's not pressed.
    # (not sure if entirely consistently.) no such issue with a regular mouse
    def on_event(self,event):
        if event.type == PLAYBACK_TIMER_EVENT:
            if self.is_playing:
                self.playing_index = (self.playing_index + 1) % len(movie.frames)

        if event.type == FADING_TIMER_EVENT:
            self.drawing_area().update_fading_mask()

        if event.type == SAVING_TIMER_EVENT:
            movie.frame(movie.pos).save()

        if event.type not in [pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION]:
            return

        x, y = event.pos

        dispatched = False
        for elem in self.elems:
            left, bottom, width, height = elem.rect
            if x>=left and x<left+width and y>=bottom and y<bottom+height:
                if not self.is_playing or isinstance(elem, TogglePlaybackButton):
                    self._dispatch_event(elem, event, x, y)
                    dispatched = True
                    break

        if not dispatched and self.focus_elem:
            self._dispatch_event(None, event, x, y)
            return

    def _dispatch_event(self, elem, event, x, y):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.is_pressed = True
            self.focus_elem = elem
            self.focus_elem.on_mouse_down(x,y)
        elif event.type == pygame.MOUSEBUTTONUP:
            self.is_pressed = False
            if self.focus_elem:
                self.focus_elem.on_mouse_up(x,y)
        elif event.type == pygame.MOUSEMOTION and self.is_pressed:
            if self.focus_elem:
                self.focus_elem.on_mouse_move(x,y)

    def drawing_area(self):
        assert isinstance(self.elems[0], DrawingArea)
        return self.elems[0]

    def timeline_area(self):
        assert isinstance(self.elems[1], TimelineArea)
        return self.elems[1]

    def toggle_playing(self):
        self.is_playing = not self.is_playing
        self.playing_index = 0
            
class DrawingArea:
    def __init__(self):
        self.fading_mask = None
        self.fading_func = None
        self.fade_per_frame = 0
        self.last_update_time = 0
        self.ymargin = WIDTH * 3
        self.xmargin = WIDTH * 3
        self.render_surface = None
        self.iwidth = 0
        self.iheight = 0
    def _internal_layout(self):
        if self.iwidth and self.iheight:
            return
        left, bottom, width, height = self.rect
        self.iwidth, self.iheight = scale_and_preserve_aspect_ratio(IWIDTH, IHEIGHT, width - self.xmargin*2, height - self.ymargin*2)
        self.xmargin = round((width - self.iwidth)/2)
        self.ymargin = round((height - self.iheight)/2)
        self.xscale = IWIDTH/self.iwidth
        self.yscale = IHEIGHT/self.iheight
    def xy2frame(self, x, y):
        return (x - self.xmargin)*self.xscale, (y - self.ymargin)*self.yscale
    def scale(self, surface): return scale_image(surface, self.iwidth, self.iheight)
    def set_fading_mask(self, fading_mask): self.fading_mask = self.scale(fading_mask)
    def draw(self):
        drawing_area_draw_timer.start()

        self._internal_layout()
        left, bottom, width, height = self.rect
        if not layout.is_playing:
            pygame.draw.rect(self.subsurface, MARGIN, (0, 0, width, self.ymargin))
            pygame.draw.rect(self.subsurface, MARGIN, (0, 0, self.xmargin, height))
            pygame.draw.rect(self.subsurface, MARGIN, (width-self.xmargin, 0, self.xmargin, height))
            pygame.draw.rect(self.subsurface, MARGIN, (0, height-self.ymargin, width, self.ymargin))

        pos = layout.playing_index if layout.is_playing else movie.pos
        highlight = not layout.is_playing and not movie.curr_layer().locked
        starting_point = (self.xmargin, self.ymargin)
        self.subsurface.blit(movie.curr_bottom_layers_surface(pos, highlight=highlight, width=self.iwidth, height=self.iheight), starting_point)
        if movie.layers[movie.layer_pos].visible:
            scaled_layer = movie.get_thumbnail(pos, self.iwidth, self.iheight, transparent_single_layer=movie.layer_pos)
            self.subsurface.blit(scaled_layer, starting_point)
        self.subsurface.blit(movie.curr_top_layers_surface(pos, highlight=highlight, width=self.iwidth, height=self.iheight), starting_point)

        if not layout.is_playing:
            mask = layout.timeline_area().combined_light_table_mask()
            if mask:
                self.subsurface.blit(mask, starting_point)
            if self.fading_mask:
                self.subsurface.blit(self.fading_mask, starting_point)

        drawing_area_draw_timer.stop()

    def update_fading_mask(self):
        if not self.fading_mask:
            return
        now = time.time_ns()
        ignore_event = (now - self.last_update_time) // 10**6 < (1000 / (FRAME_RATE*2))
        self.last_update_time = now

        if ignore_event:
            return

        alpha = self.fading_mask.get_alpha()
        if alpha == 0:
            self.fading_mask = None
            self.fading_func = None
            return

        if not self.fading_func:
            alpha -= self.fade_per_frame
        else:
            alpha = self.fading_func(alpha, self.fade_per_frame)
        self.fading_mask.set_alpha(max(0,alpha))

    def fix_xy(self,x,y):
        left, bottom, _, _ = self.rect
        return (x-left), (y-bottom)
    def on_mouse_down(self,x,y):
        layout.tool.on_mouse_down(*self.fix_xy(x,y))
    def on_mouse_up(self,x,y):
        left, bottom, _, _ = self.rect
        layout.tool.on_mouse_up(*self.fix_xy(x,y))
    def on_mouse_move(self,x,y):
        left, bottom, _, _ = self.rect
        layout.tool.on_mouse_move(*self.fix_xy(x,y))

class TimelineArea:
    def _calc_factors(self):
        _, _, width, height = self.rect
        factors = [0.7,0.6,0.5,0.4,0.3,0.2,0.15]
        scale = 1
        mid_scale = 1
        step = 0.5
        mid_width = IWIDTH * height / IHEIGHT
        def scaled_factors(scale):
            return [min(1, max(0.15, f*scale)) for f in factors]
        def slack(scale):
            total_width = mid_width*mid_scale + 2 * sum([int(mid_width)*f for f in scaled_factors(scale)])
            return width - total_width
        prev_slack = None
        iteration = 0
        while iteration < 1000:
            opt = [scale+step, scale-step, scale+step/2, scale-step/2]
            slacks = [abs(slack(s)) for s in opt]
            best_slack = min(slacks)
            best_opt = opt[slacks.index(best_slack)]

            step = best_opt - scale
            scale = best_opt

            curr_slack = slack(scale)
            def nice_fit(): return curr_slack >= 0 and curr_slack < 2
            if nice_fit():
                break
            
            sf = scaled_factors(scale)
            if min(sf) == 1: # grown as big as we will allow?
                break

            if max(sf) == 0.15: # grown as small as we will allow? try shrinking the middle thumbnail
                while not nice_fit() and mid_scale > 0.15:
                    mid_scale = max(scale-0.1, 0.15)
                    curr_slack = slack(scale)
                break # can't do much if we still don't have a nice fit

            iteration += 1
            
        self.factors = scaled_factors(scale)
        self.mid_factor = mid_scale

    def init(self):
        # stuff for drawing the timeline
        self.frame_boundaries = []
        self.eye_boundaries = []
        self.prevx = None

        self._calc_factors()

        eye_icon_size = int(screen.get_width() * 0.15*0.14)
        self.eye_open = scale_image(pg.image.load('light_on.png'), eye_icon_size)
        self.eye_shut = scale_image(pg.image.load('light_off.png'), eye_icon_size)

        self.loop_icon = scale_image(pg.image.load('loop.png'), int(screen.get_width()*0.15*0.14))
        self.arrow_icon = scale_image(pg.image.load('arrow.png'), int(screen.get_width()*0.15*0.2))

        self.no_hold = scale_image(pg.image.load('no_hold.png'), int(screen.get_width()*0.15*0.25))
        self.hold_active = scale_image(pg.image.load('hold_yellow.png'), int(screen.get_width()*0.15*0.25))
        self.hold_inactive = scale_image(pg.image.load('hold_grey.png'), int(screen.get_width()*0.15*0.25))

        # stuff for light table [what positions are enabled and what the resulting
        # mask to be rendered together with the current frame is]
        self.on_light_table = {}
        for pos_dist in range(-len(self.factors),len(self.factors)+1):
            self.on_light_table[pos_dist] = False
        self.on_light_table[-1] = True
        # the order in which we traverse the masks matters, for one thing,
        # because we might cover the same position distance from movie.pos twice
        # due to wraparound, and we want to decide if it's covered as being
        # "before" or "after" movie pos [it affects the mask color]
        self.traversal_order = []
        for pos_dist in range(1,len(self.factors)+1):
            self.traversal_order.append(-pos_dist)
            self.traversal_order.append(pos_dist)

        self.loop_mode = False

        self.toggle_hold_boundaries = (0,0,0,0)
        self.loop_boundaries = (0,0,0,0)

    def light_table_positions(self):
        # TODO: order 
        covered_positions = {movie.pos} # the current position is definitely covered,
        # don't paint over it...

        num_enabled_pos = sum([enabled for pos_dist, enabled in self.on_light_table.items() if pos_dist>0])
        num_enabled_neg = sum([enabled for pos_dist, enabled in self.on_light_table.items() if pos_dist<0])
        curr_pos = 0
        curr_neg = 0
        for pos_dist in self.traversal_order:
            if not self.on_light_table[pos_dist]:
                continue
            abs_pos = movie.pos + pos_dist
            if not self.loop_mode and (abs_pos < 0 or abs_pos >= len(movie.frames)):
                continue
            pos = abs_pos % len(movie.frames)
            if pos in covered_positions:
                continue # for short movies, avoid covering the same position twice
                # upon wraparound
            covered_positions.add(pos)
            if pos_dist > 0:
                curr = curr_pos
                num = num_enabled_pos
                curr_pos += 1
            else:
                curr = curr_neg
                num = num_enabled_neg
                curr_neg += 1
            brightness = int((200 * (num - curr - 1) / (num - 1)) + 55 if num > 1 else 255)
            color = (brightness,0,0) if pos_dist < 0 else (0,int(brightness*0.7),0)
            transparency = 0.3
            yield (pos, color, transparency)

    def combined_light_table_mask(self):
        class CachedCombinedMask:
            def compute_key(_):
                id2version = []
                computation = []
                for pos, color, transparency in self.light_table_positions():
                    i2v, c = movie.get_mask(pos, color, transparency, key=True)
                    id2version += i2v
                    computation.append(c)
                return tuple(id2version), ('combined-mask', tuple(computation))
                
            def compute_value(_):
                masks = []
                for pos, color, transparency in self.light_table_positions():
                    masks.append(movie.get_mask(pos, color, transparency))
                scale = layout.drawing_area().scale
                if len(masks) == 0:
                    return None
                elif len(masks) == 1:
                    return scale(masks[0])
                else:
                    mask = masks[0].copy()
                    alphas = []
                    for m in masks[1:]:
                        alphas.append(m.get_alpha())
                        m.set_alpha(255) # TODO: this assumes the same transparency in all masks - might want to change
                    mask.blits([(m, (0, 0), (0, 0, mask.get_width(), mask.get_height())) for m in masks[1:]])
                    for m,a in zip(masks[1:],alphas):
                        m.set_alpha(a)
                    return scale(mask)

        return cache.fetch(CachedCombinedMask())

    def x2frame(self, x):
        for left, right, pos in self.frame_boundaries:
            if x >= left and x <= right:
                return pos
    def draw(self):
        timeline_area_draw_timer.start()

        left, bottom, width, height = self.rect
        left = 0
        frame_width = movie.curr_frame().get_width()
        frame_height = movie.curr_frame().get_height()
        #thumb_width = movie.curr_frame().get_width() * height // movie.curr_frame().get_height()
        x = left
        i = 0

        factors = self.factors
        self.frame_boundaries = []
        self.eye_boundaries = []

        def draw_frame(pos, pos_dist, x, thumb_width):
            scaled = movie.get_thumbnail(pos, thumb_width, height)
            self.subsurface.blit(scaled, (x, bottom), (0, 0, thumb_width, height))
            border = 1 + 2*(pos==movie.pos)
            pygame.draw.rect(self.subsurface, PEN, (x, bottom, thumb_width, height), border)
            self.frame_boundaries.append((x, x+thumb_width, pos))
            if pos != movie.pos:
                eye = self.eye_open if self.on_light_table.get(pos_dist, False) else self.eye_shut
                eye_x = x + 2 if pos_dist > 0 else x+thumb_width-eye.get_width() - 2
                self.subsurface.blit(eye, (eye_x, bottom), eye.get_rect())
                self.eye_boundaries.append((eye_x, bottom, eye_x+eye.get_width(), bottom+eye.get_height(), pos_dist))
            elif len(movie.frames)>1:
                mode_x = x + 2
                mode = self.loop_icon if self.loop_mode else self.arrow_icon
                self.subsurface.blit(mode, (mode_x, bottom), mode.get_rect())
                self.loop_boundaries = (mode_x, bottom, mode_x+mode.get_width(), bottom+mode.get_height())

        def thumb_width(factor):
            return int((frame_width * height // frame_height) * factor)

        # current frame
        curr_frame_width = thumb_width(self.mid_factor)
        centerx = (left+width)/2
        draw_frame(movie.pos, 0, centerx - curr_frame_width/2, curr_frame_width)

        # next frames
        x = centerx + curr_frame_width/2
        i = 0
        pos = movie.pos + 1
        while True:
            if i >= len(factors):
                break
            if not self.loop_mode and pos >= len(movie.frames):
                break
            if pos >= len(movie.frames): # went past the last frame
                pos = 0
            if pos == movie.pos: # gone all the way back to the current frame
                break
            ith_frame_width = thumb_width(factors[i])
            draw_frame(pos, i+1, x, ith_frame_width)
            x += ith_frame_width
            pos += 1
            i += 1

        # previous frames
        x = centerx - curr_frame_width/2
        i = 0
        pos = movie.pos - 1
        while True:
            if i >= len(factors):
                break
            if not self.loop_mode and pos < 0:
                break
            if pos < 0: # went past the first frame
                pos = len(movie.frames) - 1
            if pos == movie.pos: # gone all the way back to the current frame
                break
            ith_frame_width = thumb_width(factors[i])
            x -= ith_frame_width
            draw_frame(pos, -i-1, x, ith_frame_width)
            pos -= 1
            i += 1

        self.draw_hold()

        timeline_area_draw_timer.stop()

    def draw_hold(self):
        left, bottom, width, height = self.rect
        # sort by position for nicer looking occlusion between adjacent icons
        for left, right, pos in sorted(self.frame_boundaries, key=lambda x: x[2]):
            if pos == 0:
                continue # can't toggle hold at frame 0
            if movie.frames[pos].hold:
                hold = self.hold_active if pos == movie.pos else self.hold_inactive
            elif pos == movie.pos:
                hold = self.no_hold
            else:
                continue
            hold_left = left-hold.get_width()/2
            hold_bottom = bottom+height-hold.get_height()
            self.subsurface.blit(hold, (hold_left, hold_bottom), hold.get_rect())
            if pos == movie.pos:
                self.toggle_hold_boundaries = (hold_left, hold_bottom, hold_left+hold.get_width(), hold_bottom+hold.get_height())

    def update_on_light_table(self,x,y):
        for left, bottom, right, top, pos_dist in self.eye_boundaries:
            if y >= bottom and y <= top and x >= left and x <= right:
                self.on_light_table[pos_dist] = not self.on_light_table[pos_dist]
                return True

    def update_loop_mode(self,x,y):
        left, bottom, right, top = self.loop_boundaries
        if y >= bottom and y <= top and x >= left and x <= right:
            self.loop_mode = not self.loop_mode
            return True

    def update_hold(self,x,y):
        if len(movie.frames) <= 1:
            return
        left, bottom, right, top = self.toggle_hold_boundaries
        if y >= bottom and y <= top and x >= left and x <= right:
            toggle_frame_hold()
            return True

    def new_delete_tool(self): return isinstance(layout.tool, NewDeleteTool) 

    def fix_x(self,x):
        left, _, _, _ = self.rect
        return x-left
    def on_mouse_down(self,x,y):
        timeline_down_timer.start()
        self._on_mouse_down(x,y)
        timeline_down_timer.stop()
    def _on_mouse_down(self,x,y):
        x = self.fix_x(x)
        self.prevx = None
        if self.new_delete_tool():
            if self.x2frame(x) == movie.pos:
                layout.tool.frame_func()
                restore_tool() # we don't want multiple clicks in a row to delete lots of frames etc
            return
        if self.update_on_light_table(x,y):
            return
        if self.update_loop_mode(x,y):
            return
        if self.update_hold(x,y):
            return
        self.prevx = x
    def on_mouse_up(self,x,y):
        self.on_mouse_move(x,y)
    def on_mouse_move(self,x,y):
        timeline_move_timer.start()
        self._on_mouse_move(x,y)
        timeline_move_timer.stop()
    def _on_mouse_move(self,x,y):
        self.redraw = False
        x = self.fix_x(x)
        if self.prevx is None:
            return
        if self.new_delete_tool():
            return
        prev_pos = self.x2frame(self.prevx)
        curr_pos = self.x2frame(x)
        if prev_pos is None and curr_pos is None:
            self.prevx = x
            return
        if curr_pos is not None and prev_pos is not None:
            pos_dist = prev_pos - curr_pos
        else:
            pos_dist = -1 if x > self.prevx else 1
        self.prevx = x
        if pos_dist != 0:
            self.redraw = True
            append_seek_frame_history_item_if_frame_is_dirty()
            if self.loop_mode:
                new_pos = (movie.pos + pos_dist) % len(movie.frames)
            else:
                new_pos = min(max(0, movie.pos + pos_dist), len(movie.frames)-1)
            movie.seek_frame(new_pos)

class LayersArea:
    def init(self):
        left, bottom, width, height = self.rect
        max_height = height / MAX_LAYERS
        max_width = IWIDTH * (max_height / IHEIGHT)
        self.width = min(max_width, width)
        self.thumbnail_height = int(self.width * IHEIGHT / IWIDTH)

        self.prevy = None
        self.color_images = {}
        icon_height = min(int(screen.get_width() * 0.15*0.14), self.thumbnail_height / 2)
        self.eye_open = scale_image(pg.image.load('eye_open.png'), height=icon_height)
        self.eye_shut = scale_image(pg.image.load('eye_shut.png'), height=icon_height)
        self.light_on = scale_image(pg.image.load('light_on.png'), height=icon_height)
        self.light_off = scale_image(pg.image.load('light_off.png'), height=icon_height)
        self.locked = scale_image(pg.image.load('locked.png'), height=icon_height)
        self.unlocked = scale_image(pg.image.load('unlocked.png'), height=icon_height)
        self.eye_boundaries = []
        self.lit_boundaries = []
        self.lock_boundaries = []
    
    def cached_image(self, layer_pos, layer):
        class CachedLayerThumbnail(CachedItem):
            def __init__(s, color=None):
                s.color = color
            def compute_key(s):
                frame = layer.frame(movie.pos) # note that we compute the thumbnail even if the layer is invisible
                return (frame.cache_id_version(),), ('colored-layer-thumbnail', self.width, s.color)
            def compute_value(se):
                if se.color is None:
                    return movie.get_thumbnail(movie.pos, self.width, self.thumbnail_height, transparent_single_layer=layer_pos)
                image = cache.fetch(CachedLayerThumbnail()).copy()
                si = pg.Surface((image.get_width(), image.get_height()), pg.SRCALPHA)
                s = pg.Surface((image.get_width(), image.get_height()), pg.SRCALPHA)
                if not self.color_images:
                    above_image = pg.Surface((image.get_width(), image.get_height()))
                    above_image.set_alpha(128)
                    above_image.fill(LAYERS_ABOVE)
                    below_image = pg.Surface((image.get_width(), image.get_height()))
                    below_image.set_alpha(128)
                    below_image.fill(LAYERS_BELOW)
                    self.color_images = {LAYERS_ABOVE: above_image, LAYERS_BELOW: below_image}
                si.fill(BACKGROUND)
                si.blit(image, (0,0))
                si.blit(self.color_images[se.color], (0,0))
                si.set_alpha(128)
                s.blit(si, (0,0))
                return s

        if layer_pos > movie.layer_pos:
            color = LAYERS_ABOVE
        elif layer_pos < movie.layer_pos:
            color = LAYERS_BELOW
        else:
            color = None
        return cache.fetch(CachedLayerThumbnail(color))

    def draw(self):
        layers_area_draw_timer.start()

        self.eye_boundaries = []
        self.lit_boundaries = []
        self.lock_boundaries = []

        left, bottom, width, height = self.rect

        for layer_pos, layer in reversed(list(enumerate(movie.layers))):
            border = 1 + (layer_pos == movie.layer_pos)*2
            image = self.cached_image(layer_pos, layer)
            image_left = left + (width - image.get_width())/2
            pygame.draw.rect(screen, BACKGROUND, (image_left, bottom, image.get_width(), image.get_height()))
            screen.blit(image, (image_left, bottom), image.get_rect()) 
            pygame.draw.rect(screen, PEN, (image_left, bottom, image.get_width(), image.get_height()), border)

            max_border = 3
            if len(movie.frames) > 1 and layer.visible and layout.timeline_area().combined_light_table_mask():
                lit = self.light_on if layer.lit else self.light_off
                screen.blit(lit, (left + width - lit.get_width() - max_border, bottom))
                self.lit_boundaries.append((left + width - lit.get_width() - max_border, bottom, left+width, bottom+lit.get_height(), layer_pos))
               
            eye = self.eye_open if layer.visible else self.eye_shut
            screen.blit(eye, (left + width - eye.get_width() - max_border, bottom + image.get_height() - eye.get_height() - max_border))
            self.eye_boundaries.append((left + width - eye.get_width() - max_border, bottom + image.get_height() - eye.get_height() - max_border, left+width, bottom+image.get_height(), layer_pos))

            lock = self.locked if layer.locked else self.unlocked
            lock_start = bottom + self.thumbnail_height/2 - lock.get_height()/2
            screen.blit(lock, (left, lock_start))
            self.lock_boundaries.append((left, lock_start, left+lock.get_width(), lock_start+lock.get_height(), layer_pos))

            bottom += image.get_height()

        layers_area_draw_timer.stop()

    def new_delete_tool(self): return isinstance(layout.tool, NewDeleteTool)

    def y2frame(self, y):
        if not self.thumbnail_height:
            return None
        _, bottom, _, _ = self.rect
        return len(movie.layers) - ((y-bottom) // self.thumbnail_height) - 1

    def update_on_light_table(self,x,y):
        for left, bottom, right, top, layer_pos in self.lit_boundaries:
            if y >= bottom and y <= top and x >= left and x <= right:
                movie.layers[layer_pos].toggle_lit() # no undo for this - it's not a "model change" but a "view change"
                movie.clear_cache()
                return True

    def update_visible(self,x,y):
        for left, bottom, right, top, layer_pos in self.eye_boundaries:
            if y >= bottom and y <= top and x >= left and x <= right:
                layer = movie.layers[layer_pos]
                layer.toggle_visible()
                history.append_item(ToggleHistoryItem(layer.toggle_visible))
                movie.clear_cache()
                return True

    def update_locked(self,x,y):
        for left, bottom, right, top, layer_pos in self.lock_boundaries:
            if y >= bottom and y <= top and x >= left and x <= right:
                layer = movie.layers[layer_pos]
                layer.toggle_locked()
                history.append_item(ToggleHistoryItem(layer.toggle_locked))
                movie.clear_cache()
                return True

    def on_mouse_down(self,x,y):
        self.prevy = None
        if self.new_delete_tool():
            if self.y2frame(y) == movie.layer_pos:
                layout.tool.layer_func()
                restore_tool() # we don't want multiple clicks in a row to delete lots of layers
            return
        if self.update_on_light_table(x,y):
            return
        if self.update_visible(x,y):
            return
        if self.update_locked(x,y):
            return
        f = self.y2frame(y)
        if f == movie.layer_pos:
            self.prevy = y
    def on_mouse_up(self,x,y):
        self.on_mouse_move(x,y)
    def on_mouse_move(self,x,y):
        self.redraw = False
        if self.prevy is None:
            return
        if self.new_delete_tool():
            return
        prev_pos = self.y2frame(self.prevy)
        curr_pos = self.y2frame(y)
        if curr_pos is None or curr_pos < 0 or curr_pos >= len(movie.layers):
            return
        self.prevy = y
        pos_dist = curr_pos - prev_pos
        if pos_dist != 0:
            self.redraw = True
            append_seek_frame_history_item_if_frame_is_dirty()
            new_pos = min(max(0, movie.layer_pos + pos_dist), len(movie.layers)-1)
            movie.seek_layer(new_pos)

class ProgressBar:
    def __init__(self, title):
        self.title = title
        self.done = 0
        self.total = 1
        horz_margin = 0.32
        vert_margin = 0.47
        self.inner_rect = scale_rect((horz_margin, vert_margin, 1-horz_margin*2, 1-vert_margin*2))
        left, bottom, width, height = self.inner_rect
        margin = WIDTH
        self.outer_rect = (left-margin, bottom-margin, width+margin*2, height+margin*2)
        self.draw()
    def on_progress(self, done, total):
        self.done = done
        self.total = total
        self.draw()
    def draw(self):
        pg.draw.rect(screen, UNUSED, self.outer_rect)
        pg.draw.rect(screen, BACKGROUND, self.inner_rect)
        left, bottom, full_width, height = self.inner_rect
        done_width = int(full_width * (self.done/max(1,self.total)))
        pg.draw.rect(screen, PROGRESS, (left, bottom, done_width, height))
        text_surface = font.render(self.title, True, UNUSED)
        pos = ((full_width-text_surface.get_width())/2+left, (height-text_surface.get_height())/2+bottom)
        screen.blit(text_surface, pos)
        pg.display.flip()

def open_movie_with_progress_bar(clipdir):
    progress_bar = ProgressBar('Loading...')
    return Movie(clipdir, progress=progress_bar.on_progress)

class MovieList:
    def __init__(self):
        self.reload()
        self.histories = {}
        self.exporting_processes = {}
    def delete_current_history(self):
        del self.histories[self.clips[self.clip_pos]]
    def reload(self):
        self.clips = []
        self.images = []
        single_image_height = screen.get_height() * MOVIES_Y_SHARE
        for clipdir in get_clip_dirs():
            fulldir = os.path.join(WD, clipdir)
            frame_file = os.path.join(fulldir, CURRENT_FRAME_FILE)
            image = pg.image.load(frame_file) if os.path.exists(frame_file) else new_frame()
            self.images.append(scale_image(image, height=single_image_height))
            self.clips.append(fulldir)
        self.clip_pos = 0 
    def open_clip(self, clip_pos):
        if clip_pos == self.clip_pos:
            return
        global movie
        assert movie.dir == self.clips[self.clip_pos]
        movie.save_before_closing()
        self.clip_pos = clip_pos
        movie = open_movie_with_progress_bar(self.clips[clip_pos])
        movie.edited_since_export = self.export_in_progress() # if we haven't finished
        # exporting [meaning that we'll interrupt the exporting process], treat the movie as
        # "edited since export"
        self.open_history(clip_pos)
        self.interrupt_export()
    def open_history(self, clip_pos):
        global history
        history = self.histories.get(self.clips[clip_pos], History())
    def save_history(self):
        if self.clips:
            self.histories[self.clips[self.clip_pos]] = history
    def export_in_progress(self):
        if self.clips:
            clip = self.clips[self.clip_pos]
            if clip in self.exporting_processes:
                proc = self.exporting_processes[clip]
                return proc.poll() is None
    def start_export(self):
        self.interrupt_export()
        if self.clips:
            clip = self.clips[self.clip_pos]
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs = dict(creationflags=CREATE_NEW_PROCESS_GROUP) if on_windows else {}
            self.exporting_processes[clip] = subprocess.Popen([sys.executable, sys.argv[0], 'export', clip], **kwargs)
    def interrupt_export(self):
        if self.export_in_progress():
            clip = self.clips[self.clip_pos]
            proc = self.exporting_processes[clip]
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT if on_windows else signal.SIGINT)
            proc.wait()
            del self.exporting_processes[clip]
    def wait_for_all_exporting_to_finish(self):
        progress_bar = ProgressBar('Exporting...')
        progress_status = ExportProgressStatus()

        while True:
            live_clips = []
            for clip, proc in self.exporting_processes.items():
                if proc.poll() is None:
                    live_clips.append(clip)
            if not live_clips:
                break
            progress_status.update(live_clips)
            time.sleep(0.3)
            progress_bar.on_progress(progress_status.done, progress_status.total)

        self.exporting_processes = {}

class MovieListArea:
    def __init__(self):
        self.show_pos = None
        self.prevx = None
    def draw(self):
        movie_list_area_draw_timer.start()

        _, _, width, _ = self.rect
        left = 0
        first = True
        pos = self.show_pos if self.show_pos is not None else movie_list.clip_pos
        for image in movie_list.images[pos:]:
            border = 1 + first*2
            if first and pos == movie_list.clip_pos:
                try:
                    image = movie.get_thumbnail(movie.pos, image.get_width(), image.get_height(), highlight=False) 
                    self.images[pos] = image # this keeps the image correct when scrolled out of clip_pos
                    # (we don't self.reload() upon scrolling so self.images can go stale when the current
                    # clip is modified)
                except:
                    pass
            first = False
            self.subsurface.blit(image, (left, 0), image.get_rect()) 
            pygame.draw.rect(self.subsurface, PEN, (left, 0, image.get_width(), image.get_height()), border)
            left += image.get_width()
            if left >= width:
                break

        movie_list_area_draw_timer.stop()
    def new_delete_tool(self): return isinstance(layout.tool, NewDeleteTool) 
    def x2frame(self, x):
        if not movie_list.images or x is None:
            return None
        left, _, _, _ = self.rect
        return (x-left) // movie_list.images[0].get_width()
    def on_mouse_down(self,x,y):
        if self.new_delete_tool():
            if self.x2frame(x) == 0:
                layout.tool.clip_func()
                restore_tool()
            return
        self.prevx = x
        self.show_pos = movie_list.clip_pos
    def on_mouse_move(self,x,y):
        self.redraw = False
        if self.prevx is None:
            self.prevx = x # this happens eg when a new_delete_tool is used upon mouse down
            # and then the original tool is restored
            self.show_pos = movie_list.clip_pos
        if self.new_delete_tool():
            return
        prev_pos = self.x2frame(self.prevx)
        curr_pos = self.x2frame(x)
        if prev_pos is None and curr_pos is None:
            self.prevx = x
            return
        if curr_pos is not None and prev_pos is not None:
            pos_dist = prev_pos - curr_pos
        else:
            pos_dist = -1 if x > self.prevx else 1
        self.redraw = pos_dist != 0
        self.prevx = x
        self.show_pos = min(max(0, self.show_pos + pos_dist), len(movie_list.clips)-1) 
    def on_mouse_up(self,x,y):
        self.on_mouse_move(x,y)
        # opening a movie is a slow operation so we don't want it to be "too interactive"
        # (like timeline scrolling) - we wait for the mouse-up event to actually open the clip
        movie_list.open_clip(self.show_pos)
        self.prevx = None
        self.show_pos = None

class ToolSelectionButton:
    def __init__(self, tool):
        self.tool = tool
    def draw(self):
        pg.draw.rect(screen, SELECTED if self.tool is layout.full_tool else UNDRAWABLE, self.rect)
        self.tool.tool.draw(self.rect,self.tool.cursor[1])
    def on_mouse_down(self,x,y):
        set_tool(self.tool)
    def on_mouse_up(self,x,y): pass
    def on_mouse_move(self,x,y): pass

class TogglePlaybackButton(Button):
    def __init__(self, play_icon, pause_icon):
        self.play = play_icon
        self.pause = pause_icon
        self.scaled = False
    def draw(self):
        left, bottom, width, height = self.rect
        if not self.scaled:
            def scale(image):
                scaled_width, scaled_height = scale_and_preserve_aspect_ratio(image.get_width(), image.get_height(), width, height)
                return scale_image(image, scaled_width, scaled_height)
            self.play = scale(self.play)
            self.pause = scale(self.pause)
            self.scaled = True
            
        icon = self.pause if layout.is_playing else self.play
        screen.blit(icon, (left + (width-icon.get_width())/2, bottom + height - icon.get_height()))
    def on_mouse_down(self,x,y):
        toggle_playing()
    def on_mouse_up(self,x,y): pass
    def on_mouse_move(self,x,y): pass

Tool = collections.namedtuple('Tool', ['tool', 'cursor', 'chars'])

class Movie(MovieData):
    def __init__(self, dir, progress=default_progress_callback):
        iwidth, iheight = (IWIDTH, IHEIGHT)
        MovieData.__init__(self, dir, progress=progress)
        if (iwidth, iheight) != (IWIDTH, IHEIGHT):
            init_layout()
        self.edited_since_export = True # MovieList can set it safely to false - we don't know
        # if the last exporting process is done or not

    def toggle_hold(self):
        pos = self.pos
        assert pos != 0 # in loop mode one might expect to be able to hold the last frame and have it shown
        # at the next frame, but that would create another surprise edge case - what happens when they're all held?..
        # better this milder surprise...
        if self.frames[pos].hold: # this frame's surface wasn't displayed - save the one that was
            self.frame(pos).save()
        else: # this frame was displayed and now won't be - save it before displaying the held one
            self.frames[pos].save()
        self.frames[pos].hold = not self.frames[pos].hold
        self.clear_cache()
        self.save_meta()

    def frame(self, pos):
        return self.layers[self.layer_pos].frame(pos)

    def get_mask(self, pos, rgb, transparency, key=False):
        # ignore invisible layers
        layers = [layer for layer in self.layers if layer.visible]
        # ignore the layers where the frame at the current position is an alias for the frame at the requested position
        # (it's visually noisy to see the same lines colored in different colors all over)
        def lines_lit(layer): return layer.lit and layer.surface_pos(self.pos) != layer.surface_pos(pos)

        class CachedMaskAlpha:
            def compute_key(_):
                frames = [layer.frame(pos) for layer in layers]
                lines = tuple([lines_lit(layer) for layer in layers])
                return tuple([frame.cache_id_version() for frame in frames if not frame.empty()]), ('mask-alpha', lines)
            def compute_value(_):
                alpha = np.zeros((empty_frame().get_width(), empty_frame().get_height()))
                for layer in layers:
                    frame = layer.frame(pos)
                    pen = pygame.surfarray.pixels_alpha(frame.surf_by_id('lines'))
                    color = pygame.surfarray.pixels_alpha(frame.surf_by_id('color'))
                    # hide the areas colored by this layer, and expose the lines of these layer (the latter, only if it's lit and not held)
                    alpha[:] = np.minimum(255-color, alpha)
                    if lines_lit(layer):
                        alpha[:] = np.maximum(pen, alpha)
                return alpha

        class CachedMask:
            def compute_key(_):
                id2version, computation = CachedMaskAlpha().compute_key()
                return id2version, ('mask', rgb, transparency, computation)
            def compute_value(_):
                mask_surface = pygame.Surface((empty_frame().get_width(), empty_frame().get_height()), pygame.SRCALPHA)
                pygame.surfarray.pixels3d(mask_surface)[:] = np.array(rgb)
                pg.surfarray.pixels_alpha(mask_surface)[:] = cache.fetch(CachedMaskAlpha())
                mask_surface.set_alpha(int(transparency*255))
                return mask_surface

        if key:
            return CachedMask().compute_key()
        return cache.fetch(CachedMask())

    def _visible_layers_id2version(self, layers, pos, include_invisible=False):
        frames = [layer.frame(pos) for layer in layers if layer.visible or include_invisible]
        return tuple([frame.cache_id_version() for frame in frames if not frame.empty()])

    def get_thumbnail(self, pos, width, height, highlight=True, transparent_single_layer=-1):
        trans_single = transparent_single_layer >= 0
        layer_pos = self.layer_pos if not trans_single else transparent_single_layer
        def id2version(layers): return self._visible_layers_id2version(layers, pos, include_invisible=trans_single)

        class CachedThumbnail(CachedItem):
            def compute_key(_):
                if trans_single:
                    return id2version([self.layers[layer_pos]]), ('transparent-layer-thumbnail', width, height)
                else:
                    def layer_ids(layers): return tuple([layer.id for layer in layers if not layer.frame(pos).empty()])
                    hl = ('highlight', layer_ids(self.layers[:layer_pos]), layer_ids([self.layers[layer_pos]]), layer_ids(self.layers[layer_pos+1:])) if highlight else 'no-highlight'
                    return id2version(self.layers), ('thumbnail', width, height, hl)
            def compute_value(_):
                h = int(screen.get_height() * 0.15)
                w = int(h * IWIDTH / IHEIGHT)
                if w <= width and h <= height:
                    if trans_single:
                        return scale_image(self.layers[layer_pos].frame(pos).surface(), width, height)

                    s = self.curr_bottom_layers_surface(pos, highlight=highlight, width=width, height=height).copy()
                    if self.layers[self.layer_pos].visible:
                        s.blit(self.get_thumbnail(pos, width, height, transparent_single_layer=layer_pos), (0, 0))
                    s.blit(self.curr_top_layers_surface(pos, highlight=highlight, width=width, height=height), (0, 0))
                    return s
                else:
                    return scale_image(self.get_thumbnail(pos, w, h, highlight=highlight, transparent_single_layer=transparent_single_layer), width, height)

        return cache.fetch(CachedThumbnail())

    def clear_cache(self):
        layout.drawing_area().fading_mask = None

    def seek_frame_and_layer(self,pos,layer_pos):
        assert pos >= 0 and pos < len(self.frames)
        assert layer_pos >= 0 and layer_pos < len(self.layers)
        if pos == self.pos and layer_pos == self.layer_pos:
            return
        self.frame(self.pos).save()
        self.pos = pos
        self.layer_pos = layer_pos
        self.frames = self.layers[layer_pos].frames
        self.clear_cache()
        self.save_meta()

    def seek_frame(self,pos): self.seek_frame_and_layer(pos, self.layer_pos)
    def seek_layer(self,layer_pos): self.seek_frame_and_layer(self.pos, layer_pos)

    def next_frame(self): self.seek_frame((self.pos + 1) % len(self.frames))
    def prev_frame(self): self.seek_frame((self.pos - 1) % len(self.frames))

    def next_layer(self): self.seek_layer((self.layer_pos + 1) % len(self.layers))
    def prev_layer(self): self.seek_layer((self.layer_pos - 1) % len(self.layers))

    def insert_frame(self):
        frame_id = str(uuid.uuid1())
        for layer in self.layers:
            frame = Frame(self.dir, layer.id)
            frame.id = frame_id
            frame.hold = layer is not self.layers[self.layer_pos] # by default, hold the other layers' frames
            layer.frames.insert(self.pos+1, frame)
        self.next_frame()

    def insert_layer(self):
        frames = [Frame(self.dir, None, frame.id) for frame in self.frames]
        layer = Layer(frames, self.dir)
        self.layers.insert(self.layer_pos+1, layer)
        self.next_layer()

    def reinsert_frame_at_pos(self, pos, removed_frame_data):
        assert pos >= 0 and pos <= len(self.frames)
        removed_frames, first_holds = removed_frame_data
        assert len(removed_frames) == len(self.layers)
        assert len(first_holds) == len(self.layers)

        self.frame(self.pos).save()
        self.pos = pos

        for layer, frame, hold in zip(self.layers, removed_frames, first_holds):
            layer.frames[0].hold = hold
            layer.frames.insert(self.pos, frame)
            frame.save()

        self.clear_cache()
        self.save_meta()

    def reinsert_layer_at_pos(self, layer_pos, removed_layer):
        assert layer_pos >= 0 and layer_pos <= len(self.layers)
        assert len(removed_layer.frames) == len(self.frames)

        self.frame(self.pos).save()
        self.layer_pos = layer_pos

        self.layers.insert(self.layer_pos, removed_layer)
        removed_layer.undelete()

        self.clear_cache()
        self.save_meta()

    def remove_frame(self, at_pos=-1, new_pos=-1):
        if len(self.frames) <= 1:
            return

        self.clear_cache()

        if at_pos == -1:
            at_pos = self.pos
        else:
            self.frame(self.pos).save()
        self.pos = at_pos

        removed_frames = []
        first_holds = []
        for layer in self.layers:
            removed = layer.frames[self.pos]
    
            del layer.frames[self.pos]
            removed.delete()
            removed.dirty = True # otherwise reinsert_frame_at_pos() calling frame.save() will not save the frame to disk,
            # which would be bad since we just called frame.delete() to delete it from the disk

            removed_frames.append(removed)
            first_holds.append(layer.frames[0].hold)

            layer.frames[0].hold = False # could have been made true if we deleted frame 0
            # and frame 1 had hold==True - now this wouldn't make sense

        if self.pos >= len(self.frames):
            self.pos = len(self.frames)-1

        if new_pos >= 0:
            self.pos = new_pos

        self.save_meta()

        return removed_frames, first_holds

    def remove_layer(self, at_pos=-1, new_pos=-1):
        if len(self.layers) <= 1:
            return

        self.clear_cache()

        if at_pos == -1:
            at_pos = self.layer_pos
        else:
            self.frame(self.pos).save()
        self.layer_pos = at_pos

        removed = self.layers[self.layer_pos]
        del self.layers[self.layer_pos]
        removed.delete()

        if self.layer_pos >= len(self.layers):
            self.layer_pos = len(self.layers)-1

        if new_pos >= 0:
            self.layer_pos = new_pos

        self.save_meta()

        return removed

    def curr_frame(self):
        return self.frame(self.pos)

    def curr_layer(self):
        return self.layers[self.layer_pos]

    def edit_curr_frame(self):
        f = self.frame(self.pos)
        f.increment_version()
        self.edited_since_export = True
        return f

    def _set_undrawable_layers_grid(self, s):
        alpha = pg.surfarray.pixels3d(s)
        alpha[::WIDTH*3, ::WIDTH*3, :] = 0
        alpha[1::WIDTH*3, ::WIDTH*3, :] = 0
        alpha[:1:WIDTH*3, ::WIDTH*3, :] = 0
        alpha[1:1:WIDTH*3, ::WIDTH*3, :] = 0

    def curr_bottom_layers_surface(self, pos, highlight, width=None, height=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT

        class CachedBottomLayers:
            def compute_key(_):
                return self._visible_layers_id2version(self.layers[:self.layer_pos], pos), ('blit-bottom-layers' if not highlight else 'bottom-layers-highlighted', width, height)
            def compute_value(_):
                layers = self._blit_layers(self.layers[:self.layer_pos], pos, transparent=True, width=width, height=height)
                s = pg.Surface((width, height), pg.SRCALPHA)
                s.fill(BACKGROUND)
                if self.layer_pos == 0:
                    return s
                if not highlight:
                    s.blit(layers, (0, 0))
                    return s
                layers.set_alpha(128)
                below_image = pg.Surface((width, height), pg.SRCALPHA)
                below_image.set_alpha(128)
                below_image.fill(LAYERS_BELOW)
                alpha = pg.surfarray.array_alpha(layers)
                layers.blit(below_image, (0,0))
                pg.surfarray.pixels_alpha(layers)[:] = alpha
                self._set_undrawable_layers_grid(layers)
                s.blit(layers, (0,0))

                return s

        return cache.fetch(CachedBottomLayers())

    def curr_top_layers_surface(self, pos, highlight, width=None, height=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT

        class CachedTopLayers:
            def compute_key(_):
                return self._visible_layers_id2version(self.layers[self.layer_pos+1:], pos), ('blit-top-layers' if not highlight else 'top-layers-highlighted', width, height)
            def compute_value(_):
                layers = self._blit_layers(self.layers[self.layer_pos+1:], pos, transparent=True, width=width, height=height)
                if not highlight or self.layer_pos == len(self.layers)-1:
                    return layers
                layers.set_alpha(128)
                s = pg.Surface((width, height), pg.SRCALPHA)
                s.fill(BACKGROUND)
                above_image = pg.Surface((width, height), pg.SRCALPHA)
                above_image.set_alpha(128)
                above_image.fill(LAYERS_ABOVE)
                alpha = pg.surfarray.array_alpha(layers)
                layers.blit(above_image, (0,0))
                self._set_undrawable_layers_grid(layers)
                s.blit(layers, (0,0))
                pg.surfarray.pixels_alpha(s)[:] = alpha
                s.set_alpha(192)

                return s

        return cache.fetch(CachedTopLayers())

    def render_and_save_current_frame(self):
        pg.image.save(self._blit_layers(self.layers, self.pos), os.path.join(self.dir, CURRENT_FRAME_FILE))

    def garbage_collect_layer_dirs(self):
        # we don't remove deleted layers from the disk when they're deleted since if there are a lot
        # of frames, this could be slow. those deleted layers not later un-deleted by the removal ops being undone
        # will be garbage-collected here
        for f in os.listdir(self.dir):
            full = os.path.join(self.dir, f)
            if f.endswith('-deleted') and f.startswith('layer-') and os.path.isdir(full):
                shutil.rmtree(full)

    def save_and_start_export(self):
        self.frame(self.pos).save()
        self.save_meta()

        # we need this to start exporting or .pngs might not be ready
        for layer in self.layers:
            for frame in layer.frames:
                frame.wait_for_compression_to_finish()

        # remove old pngs so we don't have stale ones lying around that don't correspond to a valid frame;
        # also, we use them for getting the status of the export progress...
        for f in os.listdir(self.dir):
            if is_exported_png(f):
                os.unlink(os.path.join(self.dir, f))

        if self.edited_since_export:
            movie_list.start_export()

    def save_before_closing(self):
        self.save_and_start_export()

        movie_list.save_history()
        global history
        history = History()

        self.render_and_save_current_frame()
        self.garbage_collect_layer_dirs()

    def fit_to_resolution(self):
        for layer in self.layers:
            for frame in layer.frames:
                frame.fit_to_resolution()

class SeekFrameHistoryItem:
    def __init__(self, pos, layer_pos):
        self.pos = pos
        self.layer_pos = layer_pos
    def undo(self):
        redo = SeekFrameHistoryItem(movie.pos, movie.layer_pos)
        movie.seek_frame_and_layer(self.pos, self.layer_pos)
        return redo
    def __str__(self): return f'SeekFrameHistoryItem(restoring pos to {self.pos} and layer_pos to {self.layer_pos})'

class InsertFrameHistoryItem:
    def __init__(self, pos): self.pos = pos
    def undo(self):
        # normally remove_frame brings you to the next frame after the one you removed.
        # but when undoing insert_frame, we bring you to the previous frame after the one
        # you removed - it's the one where you inserted the frame we're now removing to undo
        # the insert, so this is where we should go to bring you back in time.
        removed_frame_data = movie.remove_frame(at_pos=self.pos, new_pos=max(0, self.pos-1))
        return RemoveFrameHistoryItem(self.pos, removed_frame_data)
    def __str__(self):
        return f'InsertFrameHistoryItem(removing at pos {self.pos})'

class RemoveFrameHistoryItem:
    def __init__(self, pos, removed_frame_data):
        self.pos = pos
        self.removed_frame_data = removed_frame_data
    def undo(self):
        movie.reinsert_frame_at_pos(self.pos, self.removed_frame_data)
        return InsertFrameHistoryItem(self.pos)
    def __str__(self):
        return f'RemoveFrameHistoryItem(inserting at pos {self.pos})'
    def byte_size(self):
        frames, holds = self.removed_frame_data
        return sum([f.size() for f in frames])

class InsertLayerHistoryItem:
    def __init__(self, layer_pos): self.layer_pos = layer_pos
    def undo(self):
        removed_layer = movie.remove_layer(at_pos=self.layer_pos, new_pos=max(0, self.layer_pos-1))
        return RemoveLayerHistoryItem(self.layer_pos, removed_layer)
    def __str__(self):
        return f'InsertLayerHistoryItem(removing layer {self.layer_pos})'

class RemoveLayerHistoryItem:
    def __init__(self, layer_pos, removed_layer):
        self.layer_pos = layer_pos
        self.removed_layer = removed_layer
    def undo(self):
        movie.reinsert_layer_at_pos(self.layer_pos, self.removed_layer)
        return InsertLayerHistoryItem(self.layer_pos)
    def __str__(self):
        return f'RemoveLayerHistoryItem(inserting layer {self.layer_pos})'
    def byte_size(self):
        return sum([f.size() for f in self.removed_layer.frames])

class ToggleHoldHistoryItem:
    def __init__(self, pos, layer_pos):
        self.pos = pos
        self.layer_pos = layer_pos
    def undo(self):
        if movie.pos != self.pos or movie.layer_pos != self.layer_pos:
            print('WARNING: wrong pos for a toggle-hold history item - expected {self.pos} layer {self.layer_pos}, got {movie.pos} layer {movie.layer_pos}')
            movie.seek_frame_and_layer(self.pos, self.layer_pos)
        movie.toggle_hold()
        return self
    def __str__(self):
        return f'ToggleHoldHistoryItem(toggling hold at frame {self.pos} layer {self.layer_pos})'

class ToggleHistoryItem:
    def __init__(self, toggle_func): self.toggle_func = toggle_func
    def undo(self):
        self.toggle_func()
        return self
    def __str__(self):
        return f'ToggleHistoryItem({self.toggle_func.__qualname__})'

def append_seek_frame_history_item_if_frame_is_dirty():
    if history.undo:
        last_op = history.undo[-1]
        if not isinstance(last_op, SeekFrameHistoryItem):
            history.append_item(SeekFrameHistoryItem(movie.pos, movie.layer_pos))

def insert_frame():
    movie.insert_frame()
    history.append_item(InsertFrameHistoryItem(movie.pos))

def insert_layer():
    movie.insert_layer()
    history.append_item(InsertLayerHistoryItem(movie.layer_pos))

def remove_frame():
    if len(movie.frames) == 1:
        return
    pos = movie.pos
    removed_frame_data = movie.remove_frame()
    history.append_item(RemoveFrameHistoryItem(pos, removed_frame_data))

def remove_layer():
    if len(movie.layers) == 1:
        return
    layer_pos = movie.layer_pos
    removed_layer = movie.remove_layer()
    history.append_item(RemoveLayerHistoryItem(layer_pos, removed_layer))

def next_frame():
    if movie.pos >= len(movie.frames)-1 and not layout.timeline_area().loop_mode:
        return
    append_seek_frame_history_item_if_frame_is_dirty()
    movie.next_frame()

def prev_frame():
    if movie.pos <= 0 and not layout.timeline_area().loop_mode:
        return
    append_seek_frame_history_item_if_frame_is_dirty()
    movie.prev_frame()

def insert_clip():
    global movie
    movie.save_before_closing()
    movie = Movie(new_movie_clip_dir())
    movie.render_and_save_current_frame() # write out CURRENT_FRAME_FILE for MovieListArea.reload...
    movie_list.reload()

def remove_clip():
    if len(movie_list.clips) <= 1:
        return # we don't remove the last clip - if we did we'd need to create a blank one,
        # which is a bit confusing. [we can't remove the last frame in a timeline, either]
    global movie
    movie.save_before_closing()
    os.rename(movie.dir, movie.dir + '-deleted')
    movie_list.delete_current_history()
    movie_list.reload()

    new_clip_pos = 0
    movie = open_movie_with_progress_bar(movie_list.clips[new_clip_pos])
    movie_list.clip_pos = new_clip_pos
    movie.edited_since_export = movie_list.export_in_progress()
    movie_list.open_history(new_clip_pos)
    movie_list.interrupt_export()

def toggle_playing(): layout.toggle_playing()

def toggle_loop_mode():
    timeline_area = layout.timeline_area()
    timeline_area.loop_mode = not timeline_area.loop_mode

def toggle_frame_hold():
    if movie.pos != 0 and not curr_layer_locked():
        movie.toggle_hold()
        history.append_item(ToggleHoldHistoryItem(movie.pos, movie.layer_pos))

def toggle_layer_lock():
    layer = movie.curr_layer()
    layer.toggle_locked()
    history.append_item(ToggleHistoryItem(layer.toggle_locked))

TOOLS = {
    'pencil': Tool(PenTool(), pencil_cursor, 'bB'),
    'eraser': Tool(PenTool(BACKGROUND, WIDTH), eraser_cursor, 'eE'),
    'eraser-medium': Tool(PenTool(BACKGROUND, MEDIUM_ERASER_WIDTH), eraser_medium_cursor, 'rR'),
    'eraser-big': Tool(PenTool(BACKGROUND, BIG_ERASER_WIDTH), eraser_big_cursor, 'tT'),
    'flashlight': Tool(FlashlightTool(), flashlight_cursor, 'fF'),
    # insert/remove frame are both a "tool" (with a special cursor) and a "function."
    # meaning, when it's used thru a keyboard shortcut, a frame is inserted/removed
    # without any more ceremony. but when it's used thru a button, the user needs to
    # actually press on the current image in the timeline to remove/insert. this,
    # to avoid accidental removes/inserts thru misclicks and a resulting confusion
    # (a changing cursor is more obviously "I clicked a wrong button, I should click
    # a different one" than inserting/removing a frame where you need to undo but to
    # do that, you need to understand what just happened)
    'insert-frame': Tool(NewDeleteTool(insert_frame, insert_clip, insert_layer), blank_page_cursor, ''),
    'remove-frame': Tool(NewDeleteTool(remove_frame, remove_clip, remove_layer), garbage_bin_cursor, ''),
}

FUNCTIONS = {
    'insert-frame': (insert_frame, '=+', pg.image.load('sheets.png')),
    'remove-frame': (remove_frame, '-_', pg.image.load('garbage.png')),
    'next-frame': (next_frame, '.<', None),
    'prev-frame': (prev_frame, ',>', None),
    'toggle-playing': (toggle_playing, '\r', None),
    'toggle-loop-mode': (toggle_loop_mode, 'c', None),
    'toggle-frame-hold': (toggle_frame_hold, 'h', None),
    'toggle-layer-lock': (toggle_layer_lock, 'l', None),
}

prev_tool = None
def set_tool(tool):
    global prev_tool
    prev = layout.full_tool
    layout.tool = tool.tool
    layout.full_tool = tool
    if not isinstance(prev.tool, NewDeleteTool):
        prev_tool = prev
    if tool.cursor:
        try_set_cursor(tool.cursor[0])

def restore_tool():
    set_tool(prev_tool)

def color_image(s, rgba):
    sc = s.copy()
    pixels = pg.surfarray.pixels3d(sc)
    for ch in range(3):
        pixels[:,:,ch] = (pixels[:,:,ch].astype(int)*rgba[ch])//255
    if rgba[-1] == 0:
        alphas = pg.surfarray.pixels_alpha(sc)
        alphas[:] = np.minimum(alphas[:], 255 - pixels[:,:,0])
    return sc

class Palette:
    def __init__(self, filename, rows=12, columns=3):
        s = pg.image.load(filename)
        color_hist = {}
        first_color_hit = {}
        white = (255,255,255)
        for y in range(s.get_height()):
            for x in range(s.get_width()):
                r,g,b,a = s.get_at((x,y))
                color = r,g,b
                if color not in first_color_hit:
                    first_color_hit[color] = (y / (s.get_height()/3))*s.get_width() + x
                if color != white:
                    color_hist[color] = color_hist.get(color,0) + 1

        colors = [[None for col in range(columns)] for row in range(rows)]
        colors[0] = [BACKGROUND+(0,), white+(255,), white+(255,)]
        color2popularity = dict(list(reversed(sorted(list(color_hist.items()), key=lambda x: x[1])))[:(rows-1)*columns])
        hit2color = [(first_hit, color) for color, first_hit in sorted(list(first_color_hit.items()), key=lambda x: x[1])]

        row = 1
        col = 0
        for hit, color in hit2color:
            if color in color2popularity:
                colors[row][col] = color + (255,)
                row+=1
                if row == rows:
                    row = 1
                    col += 1

        self.rows = rows
        self.columns = columns
        self.colors = colors

        self.init_cursors()

    def init_cursors(self):
        s = paint_bucket_cursor[0]
        self.cursors = [[None for col in range(self.columns)] for row in range(self.rows)]
        for row in range(self.rows):
            for col in range(self.columns):
                sc = color_image(s, self.colors[row][col])
                self.cursors[row][col] = (pg.cursors.Cursor((0,sc.get_height()-1), sc), color_image(paint_bucket_cursor[1], self.colors[row][col]))
                if self.colors[row][col][-1] == 0: # water tool
                    self.cursors[row][col] = (self.cursors[row][col][0], scale_image(pg.image.load('water-tool.png'), self.cursors[row][col][1].get_width()))


palette = Palette('palette.png')

def get_clip_dirs():
    '''returns the clip directories sorted by last modification time (latest first)'''
    wdfiles = os.listdir(WD)
    clipdirs = {}
    for d in wdfiles:
        try:
            if d.endswith('-deleted'):
                continue
            frame_order_file = os.path.join(os.path.join(WD, d), CLIP_FILE)
            s = os.stat(frame_order_file)
            clipdirs[d] = s.st_mtime
        except:
            continue

    return list(reversed(sorted(clipdirs.keys(), key=lambda d: clipdirs[d])))

MAX_LAYERS = 8

layout = None

class EmptyElem:
    def draw(self): pg.draw.rect(screen, UNUSED, self.rect)
    def on_mouse_down(self,x,y): pass
    def on_mouse_up(self,x,y): pass
    def on_mouse_move(self,x,y): pass

def init_layout():
    global layout
    global MOVIES_Y_SHARE

    vertical_movie_on_horizontal_screen = IWIDTH < IHEIGHT and screen.get_width() > 1.5*screen.get_height()

    TIMELINE_Y_SHARE = 0.15
    TOOLBAR_X_SHARE = 0.15
    LAYERS_Y_SHARE = 1-TIMELINE_Y_SHARE
    LAYERS_X_SHARE = LAYERS_Y_SHARE / MAX_LAYERS
    
    # needs to be the same in all layouts or we need to adjust thumbnails in movie_list.images
    MOVIES_Y_SHARE = TOOLBAR_X_SHARE + LAYERS_X_SHARE - TIMELINE_Y_SHARE

    if vertical_movie_on_horizontal_screen:
        DRAWING_AREA_Y_SHARE = 1
        DRAWING_AREA_X_SHARE = (screen.get_height() * (IWIDTH/IHEIGHT)) / screen.get_width()
        DRAWING_AREA_X_START = 0
        DRAWING_AREA_Y_START = 0
        timeline_rect = (DRAWING_AREA_X_SHARE, 0, 1-DRAWING_AREA_X_SHARE, TIMELINE_Y_SHARE)
        MOVIES_X_START = TOOLBAR_X_SHARE + DRAWING_AREA_X_SHARE
        MOVIES_X_SHARE = 1-TOOLBAR_X_SHARE-DRAWING_AREA_X_SHARE-LAYERS_X_SHARE
        TOOLBAR_X_START = DRAWING_AREA_X_SHARE
    else:
        DRAWING_AREA_X_SHARE = 1 - TOOLBAR_X_SHARE - LAYERS_X_SHARE
        DRAWING_AREA_Y_SHARE = DRAWING_AREA_X_SHARE # preserve screen aspect ratio
        DRAWING_AREA_X_START = TOOLBAR_X_SHARE
        DRAWING_AREA_Y_START = TIMELINE_Y_SHARE
        timeline_rect = (0, 0, 1, TIMELINE_Y_SHARE)
        # this is what MOVIES_Y_SHARE is in horizontal layouts; vertical just inherit it
        #MOVIES_Y_SHARE = 1-DRAWING_AREA_Y_SHARE-TIMELINE_Y_SHARE
        MOVIES_X_START = TOOLBAR_X_SHARE
        MOVIES_X_SHARE = 1-TOOLBAR_X_SHARE-LAYERS_X_SHARE
        TOOLBAR_X_START = 0

    MOVIES_Y_START = 1 - MOVIES_Y_SHARE
    LAYERS_X_START = 1 - LAYERS_X_SHARE
    LAYERS_Y_START = TIMELINE_Y_SHARE

    last_tool = layout.full_tool if layout else None

    layout = Layout()
    layout.add((DRAWING_AREA_X_START, DRAWING_AREA_Y_START, DRAWING_AREA_X_SHARE, DRAWING_AREA_Y_SHARE), DrawingArea())

    layout.add(timeline_rect, TimelineArea())
    layout.add((MOVIES_X_START, MOVIES_Y_START, MOVIES_X_SHARE, MOVIES_Y_SHARE), MovieListArea())
    layout.add((LAYERS_X_START, LAYERS_Y_START, LAYERS_X_SHARE, LAYERS_Y_SHARE), LayersArea())

    if vertical_movie_on_horizontal_screen:
        layout.add((MOVIES_X_START, TIMELINE_Y_SHARE, 1-LAYERS_X_SHARE-DRAWING_AREA_X_SHARE-TOOLBAR_X_SHARE, 1-TIMELINE_Y_SHARE-MOVIES_Y_SHARE), EmptyElem())

    tools_width_height = [
        ('pencil', 0.33, 1),
        ('eraser-big', 0.27, 1),
        ('eraser-medium', 0.21, 0.8),
        ('eraser', 0.15, 0.6),
    ]
    offset = 0
    for tool, width, height in tools_width_height:
        layout.add((TOOLBAR_X_START+offset*0.15,0.85+(0.15*(1-height)),width*0.15, 0.15*height), ToolSelectionButton(TOOLS[tool]))
        offset += width
    color_w = 0.025*2
    i = 0

    layout.add((TOOLBAR_X_START+color_w*2, 0.85-color_w, color_w, color_w*1.5), ToolSelectionButton(TOOLS['flashlight']))
    
    for row,y in enumerate(np.arange(0.25,0.85-0.001,color_w)):
        for col,x in enumerate(np.arange(0,0.15-0.001,color_w)):            
            if row == len(palette.colors)-1 and col == 2:
                continue
            tool = Tool(PaintBucketTool(palette.colors[len(palette.colors)-row-1][col]), palette.cursors[len(palette.colors)-row-1][col], '')
            layout.add((TOOLBAR_X_START+x,y,color_w,color_w), ToolSelectionButton(tool))
            i += 1

    funcs_width = [
        ('insert-frame', 0.33),
        ('remove-frame', 0.33),
        ('play', 0.33)
    ]
    offset = 0
    for func, width in funcs_width:
        if func == 'play':
            button = TogglePlaybackButton(pg.image.load('play.png'), pg.image.load('pause.png'))
        else:
            button = ToolSelectionButton(TOOLS[func])
        layout.add((TOOLBAR_X_START+offset*0.15,0.15,width*0.15, 0.1), button)
        offset += width

    set_tool(last_tool if last_tool else TOOLS['pencil'])

def new_movie_clip_dir():
    now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    return os.path.join(WD, now)

def default_clip_dir():
    clip_dirs = get_clip_dirs() 
    if not clip_dirs:
        # first clip - create a new directory
        return new_movie_clip_dir(), True
    else:
        return os.path.join(WD, clip_dirs[0]), False

def load_clips_dir():
    movie_dir, is_new_dir = default_clip_dir()
    global movie
    movie = Movie(movie_dir) if is_new_dir else open_movie_with_progress_bar(movie_dir)
    movie.edited_since_export = is_new_dir

    init_layout()

    global movie_list
    movie_list = MovieList()

load_clips_dir()

class SwapWidthHeightHistoryItem:
    def undo(self):
        swap_width_height(from_history=True)
        return SwapWidthHeightHistoryItem()

def swap_width_height(from_history=False):
    global IWIDTH
    global IHEIGHT
    IWIDTH, IHEIGHT = IHEIGHT, IWIDTH
    init_layout()
    movie.fit_to_resolution()
    if not from_history:
        history.append_item(SwapWidthHeightHistoryItem())

# The history is "global" for all operations within a movie. In some (rare) animation programs
# there's a history per frame. One problem with this is how to undo timeline
# operations like frame deletions or holds (do you have a separate undo function for this?)
# It's also somewhat less intuitive in that you might have long forgotten
# what you've done on some frame when you visit it and press undo one time
# too many
#
def byte_size(history_item):
    return getattr(history_item, 'byte_size', lambda: 128)()

def nop(history_item):
    return getattr(history_item, 'nop', lambda: False)()

class History:
    # a history is kept per movie. the size of the history is global - we don't
    # want to exceed a certain memory threshold for the history
    byte_size = 0
    
    def __init__(self):
        self.undo = []
        self.redo = []
        layout.drawing_area().fading_mask = None
        self.suggestions = None

    def __del__(self):
        for op in self.undo + self.redo:
            History.byte_size -= byte_size(op)

    def _merge_prev_suggestions(self):
        if self.suggestions: # merge them into one
            s = self.suggestions
            self.suggestions = None
            self.append_item(HistoryItemSet(list(reversed(s))))

    def append_suggestions(self, items):
        '''"suggestions" are multiple items taking us from a new state B to the old state A,
        for 2 suggestions - thru a single intermediate state S: B -> S -> A.

        there's a single opportunity to "accept" a suggestion by pressing 'undo' right after
        the suggestions were "made" by a call to append_suggestions(). in this case the history
        will have an item for B -> S and another one for S -> A. otherwise, the suggestions
        will be "merged" into a single B -> A HistoryItemSet (when new items or suggestions 
        are appended.)'''
        self._merge_prev_suggestions()
        if len(items) == 1:
            self.append_item(items[0])
        else:
            self.suggestions = items

    def append_item(self, item):
        if nop(item):
            return

        self._merge_prev_suggestions()

        self.undo.append(item)
        History.byte_size += byte_size(item) - sum([byte_size(op) for op in self.redo])
        self.redo = [] # forget the redo stack
        while self.undo and History.byte_size > MAX_HISTORY_BYTE_SIZE:
            History.byte_size -= byte_size(self.undo[0])
            del self.undo[0]

        layout.drawing_area().fading_mask = None # new operations invalidate old skeletons

    def undo_item(self):
        if self.suggestions:
            s = self.suggestions
            self.suggestions = None
            for item in s:
                self.append_item(item)

        if self.undo:
            last_op = self.undo[-1]
            redo = last_op.undo()
            History.byte_size += byte_size(redo) - byte_size(last_op)
            self.redo.append(redo)
            self.undo.pop()

        layout.drawing_area().fading_mask = None # changing canvas state invalidates old skeletons

    def redo_item(self):
        if self.redo:
            last_op = self.redo[-1]
            undo = last_op.undo()
            History.byte_size += byte_size(undo) - byte_size(last_op)
            self.undo.append(undo)
            self.redo.pop()

    def clear(self):
        History.byte_size -= sum([byte_size(op) for op in self.undo+self.redo])
        self.undo = []
        self.redo = []
        self.suggestions = None

def clear_history():
    history.clear()
    fading_mask = new_frame()
    text_surface = font.render("Current Clip's\nUndo/Redo History\nDeleted!", True, (255, 0, 0), (255, 255, 255))
    fading_mask.blit(text_surface, ((fading_mask.get_width()-text_surface.get_width())/2, (fading_mask.get_height()-text_surface.get_height())/2))
    fading_mask.set_alpha(255)
    drawing_area = layout.drawing_area()
    drawing_area.set_fading_mask(fading_mask)
    drawing_area.fade_per_frame = 255/(FADING_RATE*10)

history = History()

escape = False

PLAYBACK_TIMER_EVENT = pygame.USEREVENT + 1
SAVING_TIMER_EVENT = pygame.USEREVENT + 2
FADING_TIMER_EVENT = pygame.USEREVENT + 3

pygame.time.set_timer(PLAYBACK_TIMER_EVENT, 1000//FRAME_RATE) # we play back at 12 fps
pygame.time.set_timer(SAVING_TIMER_EVENT, 15*1000) # we save a copy of the current clip every 15 seconds
pygame.time.set_timer(FADING_TIMER_EVENT, 1000//FADING_RATE) # we save a copy of the current clip every 15 seconds

timer_events = [
    PLAYBACK_TIMER_EVENT,
    SAVING_TIMER_EVENT,
    FADING_TIMER_EVENT,
]

interesting_events = [
    pygame.KEYDOWN,
    pygame.MOUSEMOTION,
    pygame.MOUSEBUTTONDOWN,
    pygame.MOUSEBUTTONUP,
] + timer_events

event2timer = {}
event_names = 'KEY MOVE DOWN UP PLAYBACK SAVING FADING'.split()
for i,event in enumerate(interesting_events):
    event2timer[event] = timers.add(event_names[i])

keyboard_shortcuts_enabled = False # enabled by Ctrl-A; disabled by default to avoid "surprises"
# upon random banging on the keyboard

cut_frame_content = None

def copy_frame():
    global cut_frame_content
    cut_frame_content = movie.curr_frame().get_content()

def cut_frame():
    history_item = HistoryItemSet([HistoryItem('color'), HistoryItem('lines')])

    global cut_frame_content
    frame = movie.edit_curr_frame()
    cut_frame_content = frame.get_content()
    frame.clear()

    history_item.optimize()
    history.append_item(history_item)

def paste_frame():
    if not cut_frame_content:
        return

    history_item = HistoryItemSet([HistoryItem('color'), HistoryItem('lines')])

    movie.edit_curr_frame().set_content(cut_frame_content)

    history_item.optimize()
    history.append_item(history_item)

def export_and_open_explorer():
    movie.save_and_start_export()
    movie_list.wait_for_all_exporting_to_finish() # wait for this movie and others if we
    # were still exporting them - so that when we open explorer all the exported data is up to date
    if on_windows:
        subprocess.Popen('explorer /select,'+movie.gif_path())
    else:
        subprocess.Popen(['nautilus', '-s', movie.gif_path()])

def open_clip_dir():
    import tkinter
    import tkinter.filedialog

    dialog_subprocess = subprocess.Popen([sys.executable, sys.argv[0], 'dir-path-dialog'], stdout=subprocess.PIPE)
    output, _ = dialog_subprocess.communicate()
    # we use repr/eval because writing Unicode to sys.stdout fails
    # and so does writing the binary output of encode() without repr()
    file_path = eval(output).decode() if output.strip() else None
    global WD
    if file_path and os.path.realpath(file_path) != os.path.realpath(WD):
        movie.save_before_closing()
        movie_list.wait_for_all_exporting_to_finish()
        set_wd(file_path)
        load_clips_dir()

def process_keydown_event(event):
    ctrl = event.mod & pg.KMOD_CTRL
    shift = event.mod & pg.KMOD_SHIFT

    # Like Escape, Undo/Redo and Delete History are always available thru the keyboard [and have no other way to access them]
    if event.key == pg.K_SPACE:
        if ctrl:
            history.redo_item()
        else:
            history.undo_item()

    # Ctrl+Shift+Delete
    if event.key == pg.K_DELETE and ctrl and shift:
        clear_history()

    # Ctrl-E: export
    if ctrl and event.key == pg.K_e:
        export_and_open_explorer()

    # Ctrl-O: open a directory
    if ctrl and event.key == pg.K_o:
        open_clip_dir()

    # Ctrl-C/X/V
    if ctrl:
        if event.key == pg.K_c:
            copy_frame()
        elif event.key == pg.K_x:
            cut_frame()
        elif event.key == pg.K_v:
            paste_frame()

    # Ctrl-R: rotate
    if ctrl and event.key == pg.K_r:
        swap_width_height()

    # other keyboard shortcuts are enabled/disabled by Ctrl-A
    global keyboard_shortcuts_enabled

    if keyboard_shortcuts_enabled:
        for tool in TOOLS.values():
            if event.key in [ord(c) for c in tool.chars]:
                set_tool(tool)

        for func, chars, _ in FUNCTIONS.values():
            if event.key in [ord(c) for c in chars]:
                func()
                
    if event.key == pygame.K_a and ctrl:
        keyboard_shortcuts_enabled = not keyboard_shortcuts_enabled
        print('Ctrl-A pressed -','enabling' if keyboard_shortcuts_enabled else 'disabling','keyboard shortcuts')

layout.draw()
pygame.display.flip()

export_on_exit = True

try:
    while not escape: 
        for event in pygame.event.get():
            if event.type not in interesting_events:
                continue

            timer = event2timer[event.type]
            #if event.type not in timer_events:
            #   print(pg.event.event_name(event.type),tdiff(),event.type,pygame.key.get_mods())

            if event.type == pygame.QUIT:
                escape = True
                break

            try:
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE: # ESC pressed
                        escape = True
                        shift = event.mod & pg.KMOD_SHIFT
                        export_on_exit = not shift # don't export upon Shift+ESC [for faster development cycles]
                        break

                    if layout.is_pressed:
                        continue # ignore keystrokes (except ESC) when a mouse tool is being used

                    timer.start()
                    process_keydown_event(event)
        
                else:
                    timer.start()
                    layout.on_event(event)

                # TODO: might be good to optimize repainting beyond "just repaint everything
                # upon every event"
                if layout.is_playing or (layout.drawing_area().fading_mask and event.type == FADING_TIMER_EVENT) or event.type not in timer_events:
                    # don't repaint upon depressed mouse movement. this is important to avoid the pen
                    # lagging upon "first contact" when a mouse motion event is sent before a mouse down
                    # event at the same coordinate; repainting upon that mouse motion event loses time
                    # when we should have been receiving the next x,y coordinates
                    if event.type != pygame.MOUSEMOTION or layout.is_pressed:
                        layout.draw()
                        if not layout.is_playing:
                            cache.collect_garbage()
                    pygame.display.flip()

                took = timer.stop()
                if took > 70:
                    print(f'Slow event ({timer.name}: {took} ms, {layout.focus_elem.__class__.__name__} in focus) - printing timing data:')
                    timers.show()
                    print()

            except KeyboardInterrupt:
                print('Ctrl-C - exiting')
                escape = True
                break
            except:
                print('INTERNAL ERROR (printing and continuing)')
                import traceback
                traceback.print_exc()
except KeyboardInterrupt:
    print('Ctrl-C - exiting')
      
timers.show()

movie.save_before_closing()
if export_on_exit:
    movie_list.wait_for_all_exporting_to_finish()
else:
    print('Shift-Escape pressed - skipping export to GIF and MP4!')

pygame.display.quit()
pygame.quit()
