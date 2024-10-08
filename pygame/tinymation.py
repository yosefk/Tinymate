import imageio.v3
import numpy as np
import sys
import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide" # don't print pygame version
os.environ['TBB_NUM_THREADS'] = '16'

on_windows = os.name == 'nt'

# a hack for pyinstaller - when we spawn a subprocess in python, we pass sys.executable
# and sys.argv[0] as the command line and python then hides its own executable from the sys.argv
# of the subprocess, but this doesn't happen with a pyinstaller produced executables
# so you end up seeing another argument at the beginning of sys.argv; this code strips it
if len(sys.argv) > 1 and sys.argv[0].endswith('.exe') and sys.argv[1].endswith('.exe'):
    sys.argv = sys.argv[1:]

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
    sys.exit()

# we use a subprocess for an open file dialog since using tkinter together with pygame
# causes issues for the latter after the first use of the former

def tkinter_dir_path_dialog():
    import tkinter
    import tkinter.filedialog

    tk_root = tkinter.Tk()
    tk_root.withdraw()  # Hide the main window

    return tkinter.filedialog.askdirectory(title="Select a Tinymation clips directory")

# tkinter is a 200MB dependency
# pygame_gui has non-trivial i18n issues
# on Windows,
#   GetOpenFileName doesn't support selecting directories
#   IFileOpenDialog involves COM, which I felt is potentially too bug-prone (at my level) to depend on
# so we use SHBrowseForFolder
def windows_dir_path_dialog():
    import win32gui, win32con
    file_types = "'Open' selects current folder\0*.xxxxxxx\0"
    fname, customfilter, flags = win32gui.GetOpenFileNameW(
        InitialDir=os.getcwd(),
        Flags=win32con.OFN_EXPLORER | win32con.OFN_NOCHANGEDIR,
        Title="Go INTO a folder and click 'Open' to select it",
        File="Go to folder",
        Filter=file_types
    )
    return os.path.dirname(fname)

def dir_path_dialog():
    if on_windows:
        file_path = windows_dir_path_dialog()
    else:
        file_path = tkinter_dir_path_dialog()
    if file_path:
        sys.stdout.write(repr(file_path.encode()))

if len(sys.argv)>1 and sys.argv[1] == 'dir-path-dialog':
    dir_path_dialog()
    sys.exit()

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
        self.locked = False
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
    def lock(self): self.locked = True
    def unlock(self): self.locked = False
    def fetch(self, cached_item): return self.fetch_kv(cached_item)[1]
    def fetch_kv(self, cached_item):
        key = (cached_item.compute_key(), (IWIDTH, IHEIGHT))
        value = self.key2value.get(key, Cache.MISS)
        if value is Cache.MISS:
            value = cached_item.compute_value()
            vsize = self.size(value)
            self.computed_bytes += vsize
            if self.locked:
                return key[0], value
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
        return key[0], value

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
        return pg.transform.smoothscale(surface, (w, h))

def new_frame():
    frame = pygame.Surface((IWIDTH, IHEIGHT), pygame.SRCALPHA)
    frame.fill(BACKGROUND)
    pg.surfarray.pixels_alpha(frame)[:] = 0
    return frame

def load_image(fname):
    s = pg.image.load(fname)
    # surfaces loaded from file have a different RGB/BGR layout - normalize it
    ret = pg.Surface((s.get_width(), s.get_height()), pg.SRCALPHA)
    pg.surfarray.pixels3d(ret)[:] = pg.surfarray.pixels3d(s)
    pg.surfarray.pixels_alpha(ret)[:] = pg.surfarray.pixels_alpha(s)
    return ret

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
                    setattr(self,surf_id,fit_to_resolution(load_image(fname)))
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

    def surface(self, roi=None):
        def sub(surface): return surface.subsurface(roi) if roi else surface
        if self.empty():
            return sub(empty_frame().color)
        s = sub(self.color).copy()
        s.blit(sub(self.lines), (0, 0))
        return s

    def thumbnail(self, width, height, roi):
        if self.empty():
            return empty_frame().color.subsurface(0, 0, width, height)
        def sub(surface): return surface.subsurface(roi)
        with thumb_timer:
            match roi:
                # for a small ROI it's better to blit lines onto color first, and then scale;
                # for a large ROI, better to scale first and then blit the smaller number of pixels.
                # (there might be a multiplier in that comparison... empirically with the implicit
                # multiplier 1, the two branches take about the same amount of time)
                case _,_,w,h if w*h < width*height:
                    with small_roi_timer:
                        s = scale_image(self.surface(roi), width, height)
                case _:
                    with large_roi_timer:
                        s = scale_image(sub(self.color), width, height)
                        s.blit(scale_image(sub(self.lines), width, height), (0, 0))
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
    def png_path(self, i): return os.path.join(os.path.realpath(self.dir), FRAME_FMT%i)

    def exported_files_exist(self):
        if not os.path.exists(self.gif_path()) or not os.path.exists(self.mp4_path()):
            return False
        for i in range(len(self.frames)):
            if not os.path.exists(self.png_path(i)):
                return False
        return True

    def _blit_layers(self, layers, pos, transparent=False, include_invisible=False, width=None, height=None, roi=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT
        if not roi: roi = (0, 0, IWIDTH, IHEIGHT)
        s = pygame.Surface((width, height), pygame.SRCALPHA)
        if not transparent:
            s.fill(BACKGROUND)
        surfaces = []
        for layer in layers:
            if not layer.visible and not include_invisible:
                continue
            if width==IWIDTH and height==IHEIGHT and roi==(0,0,IWIDTH,IHEIGHT):
                f = layer.frame(pos)
                surfaces.append(f.surf_by_id('color'))
                surfaces.append(f.surf_by_id('lines'))
            else:
                surfaces.append(movie.get_thumbnail(pos, width, height, transparent_single_layer=self.layers.index(layer), roi=roi))
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
        sys.exit()

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

# backups
import zipfile

def create_backup(on_progress):
    backup_file = os.path.join(WD, f'Tinymation-backup-{format_now()}.zip')
    zip_dir(backup_file, WD, on_progress)
    return backup_file

def zip_dir(backup_file, dirname, on_progress, rel_path_root=None):
    if rel_path_root is None:
        rel_path_root = dirname
    files_to_back_up = []
    for root, dirs, files in os.walk(dirname):
        for file in files:
            ext = file.split('.')[-1].lower() if '.' in file else None
            if ext not in ['gif','mp4','zip','bmp']:
                files_to_back_up.append(os.path.join(root, file))

    total_bytes = sum([os.path.getsize(f) for f in files_to_back_up])
    compressed_bytes = 0

    with zipfile.ZipFile(backup_file, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
        for file_path in files_to_back_up:
            relative_path = os.path.relpath(file_path, rel_path_root) 
            zipf.write(file_path, relative_path)

            compressed_bytes += os.path.getsize(file_path)
            on_progress(compressed_bytes, total_bytes)

# we do NOT overwrite already existing files - the user needs to actively delete them.
def unzip_files(zip_file, on_progress):
    with zipfile.ZipFile(zip_file, 'r') as zip_ref:
        files = zip_ref.infolist()
        total_bytes = sum([f.file_size for f in files])

        for f in files:
            if not os.path.exists(os.path.join(WD, f.filename)):
                zip_ref.extract(f, WD)
    
# logging

if on_windows:
    import winpath
    MY_DOCUMENTS = winpath.get_my_documents()
else:
    MY_DOCUMENTS = os.path.expanduser('~')

def set_wd(wd):
    global WD
    WD = wd
    if not os.path.exists(WD):
        os.makedirs(WD)
    
set_wd(os.path.join(MY_DOCUMENTS if MY_DOCUMENTS else '.', 'Tinymation'))

LOGDIR = os.path.join(MY_DOCUMENTS if MY_DOCUMENTS else '.', 'Logs-Tinymation')
if not os.path.exists(LOGDIR):
    os.makedirs(LOGDIR)

import datetime
def format_now(): return datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

import logging
import time
from logging.handlers import RotatingFileHandler

class StreamLogger:
    def __init__(self, stream, logger):
        self.stream = stream
        self.logger = logger
        self.lastline = ''
    def write(self, buf):
        self.stream.write(buf)
        if '\n' in buf:
            self.stream.flush()
        self.logger.log(logging.INFO, buf)
    def flush(self):
        self.stream.flush()
        
def make_rotating_logger(name, max_bytes):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(os.path.join(LOGDIR, name+'.txt'), maxBytes=max_bytes, backupCount=5)
    handler.terminator = ''
    logger.addHandler(handler)
    return logger

prints_logger = make_rotating_logger('prints', max_bytes=1024**2)
sys.stdout = StreamLogger(sys.stdout, prints_logger)
sys.stderr = StreamLogger(sys.stderr, prints_logger)

events_logger = make_rotating_logger('events', max_bytes=1024**2)
last_event_us = 0
def log_event(d):
    global last_event_us
    now = int(time.time_ns() * 0.001)
    d['usd'] = now - last_event_us
    last_event_us = now
    events_logger.log(logging.INFO, json.dumps(d)+'\n')

print('>>> STARTING',format_now())

class ReplayedEvent:
    def __init__(self, d):
        self.__dict__ = d
        self.rep = 1
class ReplayedEventLog:
    def __init__(self, width, height, events):
        self.width = width
        self.height = height
        self.events = events
    def screen_dimensions(self): return self.width, self.height

def parse_replay_log():
    logfiles = reversed(sorted([f for f in os.listdir(LOGDIR) if f.startswith('events.txt')]))
    log = sum([open(os.path.join(LOGDIR, f)).read().splitlines() for f in logfiles], [])
    start = None
    for i, line in enumerate(log):
        if 'STARTING' in line:
            start = i
    if start is None:
        raise Exception('failed to parse the event log')
    d = json.loads(log[start])
    events = [ReplayedEvent(json.loads(line)) for line in log[start+1:]]
    replayed = ReplayedEventLog(d['width'], d['height'], events)
    return replayed

# Student server & teacher client: turn screen on/off, save/restore backups

import threading
import socket
from zeroconf import ServiceInfo, Zeroconf
from http.server import BaseHTTPRequestHandler, HTTPServer
import getpass, getmac
import base64

class StudentRequestHandler(BaseHTTPRequestHandler):
    def do_PUT(self):
        try:
            if not self.path.startswith('/put/'):
                raise Exception("bad PUT path")
            fname = os.path.join(WD, self.path[len('/put/'):])
            if not os.path.exists(fname):
                size = int(self.headers['Content-Length'])
                data = self.rfile.read(size)
                with open(fname, 'wb') as f:
                    f.write(data)

                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'PUT request processed successfully')
                return
        except:
            import traceback
            traceback.print_exc()

        self.send_response(500)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(bytes(f'failed to handle PUT path: {self.path}', 'utf-8'))

    def do_GET(self):
        response = 404
        message = f'Unknown path: {self.path}'

        try:
            if self.path == '/lock':
                student_server.lock_screen = True
                pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))
                message = 'Screen locked'
                response = 200
            elif self.path == '/unlock':
                student_server.lock_screen = False
                pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))
                message = 'Screen unlocked'
                response = 200
            elif self.path == '/drawing_layout':
                layout.mode = DRAWING_LAYOUT
                pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))
                message = 'Layout set to drawing'
                response = 200
            elif self.path == '/animation_layout':
                layout.mode = ANIMATION_LAYOUT
                pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))
                message = 'Layout set to animation'
                response = 200
            elif self.path == '/mac':
                message = str(getmac.get_mac_address())
                response = 200
            elif self.path == '/backup':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()

                def on_progress(compressed, total):
                    self.wfile.write(bytes(f'{compressed} {total} <br>\n', "utf8"))
                abspath = create_backup(on_progress)

                backup_props = {}
                backup_props['size'] = os.path.getsize(abspath)
                
                user = getpass.getuser()
                host = student_server.host
                mac = getmac.get_mac_address()

                # we deliberately rename here and not on the client since if a computer keeps the files
                # across sessions, it will save us a transfer when restoring the backup to have the file
                # already stored with the name the server will use to restore it
                file = os.path.basename(abspath)
                fname = f'student-{user}@{host}-{mac}-{file}'.replace(':','_')
                shutil.move(abspath, os.path.join(os.path.dirname(abspath), fname))

                backup_props['file'] = fname

                message = json.dumps(backup_props)
                self.wfile.write(bytes(message, "utf8"))
                return
            elif self.path.startswith('/file/'):
                fpath = self.path[len('/file/'):]
                fpath = os.path.join(WD, fpath)
                if os.path.exists(fpath):
                    response = 200
                    with open(fpath, 'rb') as f:
                        data = f.read()
                    data64 = base64.b64encode(data)
                    self.send_response(response)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    chunk = 64*1024
                    for i in range(0, len(data64), chunk):
                        self.wfile.write(data64[i:i+chunk]+b'\n')
                    return
            elif self.path.startswith('/unzip/'):
                fpath = self.path[len('/unzip/'):]
                fpath = os.path.join(WD, fpath)
                if os.path.exists(fpath):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()

                    def on_progress(uncompressed, total):
                        self.wfile.write(bytes(f'{uncompressed} {total} <br>\n', "utf8"))
                    unzip_files(fpath, on_progress)
                    pg.event.post(pg.Event(RELOAD_MOVIE_LIST_EVENT))
                    return

        except Exception:
            import traceback
            traceback.print_exc()
            message = f'internal error handling {self.path}'
            response = 500

        self.send_response(response)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(bytes(message, "utf8"))

class StudentServer:
    def __init__(self):
        self.lock_screen = False

        self.host = socket.gethostname()
        self.host_addr = socket.gethostbyname(self.host)
        self.port = 8080
        self.zeroconf = Zeroconf()
        self.service_info = ServiceInfo(
            "_http._tcp.local.",
            f"Tinymation.{self.host}.{self.host_addr}._http._tcp.local.",
            addresses=[socket.inet_aton(self.host_addr)],
            port=self.port)
        self.zeroconf.register_service(self.service_info)
        print(f"Student server running on {self.host}[{self.host_addr}]:{self.port}")

        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        server_address = ('', self.port)
        self.httpd = HTTPServer(server_address, StudentRequestHandler)
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.thread.join()
        self.zeroconf.unregister_service(self.service_info)

student_server = StudentServer()

from zeroconf import ServiceBrowser
import http.client

class StudentThreads:
    def __init__(self, students, title):
        self.threads = []
        self.done = []
        self.students = students
        self.student2progress = {}
        self.progress_bar = ProgressBar(title)

    def run_thread_func(self, student, conn):
        try:
            self.student_thread(student, conn)
        except:
            import traceback
            traceback.print_exc()
        finally:
            self.done.append(student)
            conn.close()

    def thread_func(self, student, conn):
        def thread():
            return self.run_thread_func(student, conn)
        return thread

    def student_thread(self, student, conn): pass 

    def start_thread_per_student(self):
        for student in self.students:
            host, port = self.students[student]
            conn = http.client.HTTPConnection(host, port)

            thread = threading.Thread(target=self.thread_func(student, conn))
            thread.start()
            self.threads.append(thread)

    def wait_for_all_threads(self):
        while len(self.done) < len(self.students):
            progress = self.student2progress.copy().values()
            done = sum([p[0] for p in progress])
            total = max(1, sum([p[1] for p in progress]))
            self.progress_bar.on_progress(done, total)
            time.sleep(0.3)

        for thread in self.threads:
            thread.join()

class TeacherClient:
    def __init__(self):
        self.students = {}
        self.screens_locked = False
    
        self.zeroconf = Zeroconf()
        self.browser = ServiceBrowser(self.zeroconf, "_http._tcp.local.", self)

    def remove_service(self, zeroconf, type, name):
        if name in self.students:
            del self.students[name]
            pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))

    def add_service(self, zeroconf, type, name):
        if name.startswith('Tinymation'):
            info = zeroconf.get_service_info(type, name)
            if info:
                host, port = socket.inet_ntoa(info.addresses[0]), info.port
                self.students[name] = (host, port)
                pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))

                # if the screens are supposed to be locked and a student restarted the program or just came
                # and started it, we want the screen locked, even at the cost of interrupting whatever
                # the teacher is doing. if on the other hand the student screens are locked and the teacher
                # restarted the program (a rare event), the teacher can unlock explicitly and will most
                # certainly do so, so we don't want to have a bunch of unlocking happen automatically
                # as the teacher's program discovers the live student programs.
                if self.screens_locked:
                    self.broadcast_request('/lock', 'Locking 1...', [name])
                    pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))

                if layout.mode == DRAWING_LAYOUT:
                    self.broadcast_request('/drawing_layout', 'Drawing layout 1...', [name])
                    pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))

    def update_service(self, zeroconf, type, name):
        num_students = len(self.students)
        if name.startswith('Tinymation'):
            info = zeroconf.get_service_info(type, name)
            if info:
                host, port = socket.inet_ntoa(info.addresses[0]), info.port
                if (host, port) != self.students[name]:
                    self.students[name] = (host, port)
            else:
                del self.students[name]
        if num_students != len(self.students):
            pg.event.post(pg.Event(REDRAW_LAYOUT_EVENT))

    def send_request(self, student, url):
        host, port = self.students[student]
        conn = http.client.HTTPConnection(host, port)
        headers = {'Content-Type': 'text/html'}
        conn.request('GET', url, headers=headers)
        
        response = conn.getresponse()
        status = response.status
        message = response.read().decode()
        conn.close()
        return status, message

    def broadcast_request(self, url, progress_bar_title, students):
        # a big reason for the progress bar is, when a student computer hybernates [for example],
        # remove_service isn't called, and it takes a while to reach a timeout. TODO: see what needs
        # to be done to improve student machine hybernation and waking up from it
        progress_bar = ProgressBar(progress_bar_title)
        responses = {}
        for i, student in enumerate(students):
            responses[student] = self.send_request(student, url)
            progress_bar.on_progress(i+1, len(students))
        return responses

    # locking and unlocking deliberately locks up the teacher's main thread - you want to know the students'
    # screen state, eg you don't want to keep going when some of their screens aren't locked
    def lock_screens(self):
        students = self.students.keys()
        self.broadcast_request('/lock', f'Locking {len(students)}...', students)
        self.screens_locked = True
    def unlock_screens(self):
        students = self.students.keys()
        self.broadcast_request('/unlock', f'Unlocking {len(students)}...', students)
        self.screens_locked = False

    def drawing_layout(self):
        students = self.students.keys()
        self.broadcast_request('/drawing_layout', f'Drawing layout {len(students)}...', students)
    def animation_layout(self):
        students = self.students.keys()
        self.broadcast_request('/animation_layout', f'Drawing layout {len(students)}...', students)

    def get_backup_info(self, students):
        backup_info = {}

        class BackupInfoStudentThreads(StudentThreads):
            def student_thread(self, student, conn):
                headers = {'Content-Type': 'text/html'}
                conn.request('GET', '/backup', headers=headers)
                response = conn.getresponse()
                while True:
                    line = response.fp.readline().decode('utf-8').strip()
                    if not line:
                        break
                    print(student, line)
                    if line.endswith('<br>'):
                        self.student2progress[student] = [int(t) for t in line.split()[:2]]
                    else:
                        backup_info[student] = json.loads(line)
                        break

        student_threads = BackupInfoStudentThreads(students, f'Saving {len(students)}+1...')
        student_threads.start_thread_per_student()

        def my_backup_thread():
            teacher_id = None
            def on_progress(compressed, total):
                student_threads.student2progress[teacher_id] = (compressed, total)
            filename = create_backup(on_progress)
            backup_info[teacher_id] = {'file':filename}

        teacher_thread = threading.Thread(target=my_backup_thread)
        teacher_thread.start()

        student_threads.wait_for_all_threads()
        teacher_thread.join()

        return backup_info

    def get_backups(self, backup_info, students):
        backup_dir = os.path.join(WD, f'Tinymation-class-backup-{format_now()}')
        try:
            os.makedirs(backup_dir)
        except:
            pass

        class BackupInfoStudentThreads(StudentThreads):
            def student_thread(self, student, conn):
                headers = {'Content-Type': 'text/html'}
                conn.request('GET', '/file/'+backup_info[student]['file'], headers=headers)

                response = conn.getresponse()
                backup_base64 = b''
                total = backup_info[student]['size']
                while True:
                    line = response.fp.readline()
                    if not line:
                        break
                    self.student2progress[student] = (len(backup_base64)*5/8, total)
                    backup_base64 += line
                    print(student, 'sent', int(len(backup_base64)*5/8), '/', total)

                data = base64.b64decode(backup_base64)
                info = backup_info[student]
                file = info['file']
                with open(os.path.join(backup_dir, file), 'wb') as f:
                    f.write(data)

                self.student2progress[student] = (total, total)
    
        student_threads = BackupInfoStudentThreads(students, f'Receiving {len(students)}...')
        student_threads.start_thread_per_student()

        teacher_id = None
        teacher_file = backup_info[teacher_id]['file']
        target_teacher_file = os.path.join(backup_dir, 'teacher-'+os.path.basename(teacher_file))
        shutil.move(teacher_file, target_teacher_file)

        student_threads.wait_for_all_threads()
        
        open_explorer(backup_dir)

    def save_class_backup(self):
        students = self.students.copy() # if someone connects past this point, we don't have their backup
        student_backups = self.get_backup_info(students)
        self.get_backups(student_backups, students)

    def restore_class_backup(self, class_backup_dir):
        if not class_backup_dir:
            return
        # ATM restores to machines based on their MAC addresses. we could also have a mode where we
        # just restore to arbitrary machines (5 backups, 6 machines - pick 5 random ones.) this could
        # be the right thing if the machines in the class are a different set every time and they
        # all erase their files. this would be more trouble if at least some machines kept the files
        # and some didn't, since given our reluctance to remove or rename existing clips, we could
        # create directories with a mix of clips from different students. if a subset of machines keep
        # their files and are the same as when the backup was made, our system of assigning by MAC
        # works well since the ones deleting the files will get them back and the rest will be unaffected
        #
        # note that we don't "restore" as in "go back in time" - if some of the clips were edited
        # after the backup was made, we don't undo these changes, since we never overwrite existing
        # files. [we can create "orphan" files this way if a frame was deleted... we would "restore" its
        # images but would not touch the movie metadata. this seems harmless enough]
        students = self.students.copy()
        responses = self.broadcast_request('/mac', 'Getting IDs...', students)
        student_macs = dict([(student, r.replace(':','_')) for (student, (s,r)) in responses.items()])
        backup_files = [os.path.join(class_backup_dir, f) for f in os.listdir(class_backup_dir)]

        student2backup = {}
        teacher_backup = None
        for student,mac in student_macs.items():
            for f in backup_files:
                if mac in f:
                    student2backup[student] = f
                    break
        for f in backup_files:
            if 'teacher-' in f:
                teacher_backup = f

        def my_backup_thread():
            if not teacher_backup:
                return
            def on_progress(uncompressed, total): pass
            unzip_files(teacher_backup, on_progress)
            pg.event.post(pg.Event(RELOAD_MOVIE_LIST_EVENT))

        teacher_thread = threading.Thread(target=my_backup_thread)
        teacher_thread.start()

        self.put(student2backup, students)
        self.unzip(student2backup, students)

        teacher_thread.join()

    def put(self, student2file, students):
        class PutThreads(StudentThreads):
            def student_thread(self, student, conn):
                if student not in student2file:
                    return
                file = student2file[student]
                with open(file, 'rb') as f:
                    data = f.read()

                conn.putrequest('PUT', '/put/'+os.path.basename(file))
                conn.putheader('Content-Length', str(len(data)))
                conn.putheader('Content-Type', 'application/octet-stream')
                conn.endheaders()

                chunk_size = 64*1024
                for i in range(0, len(data), chunk_size):
                    conn.send(data[i:i+chunk_size])
                    self.student2progress[student] = i, len(data)
                self.student2progress[student] = len(data), len(data)

                response = conn.getresponse()
                response.read()

        student_threads = PutThreads(students, f'Sending {len(students)}...')
        student_threads.start_thread_per_student()
        student_threads.wait_for_all_threads()

    def unzip(self, student2file, students):
        class UnzipThreads(StudentThreads):
            def student_thread(self, student, conn):
                if student not in student2file:
                    return
                zip_file = student2file[student]
                headers = {'Content-Type': 'text/html'}
                conn.request('GET', '/unzip/'+os.path.basename(zip_file), headers=headers)
                response = conn.getresponse()
                while True:
                    line = response.fp.readline().decode('utf-8').strip()
                    if not line:
                        break
                    if line.endswith('<br>'):
                        self.student2progress[student] = [int(t) for t in line.split()[:2]]

        student_threads = UnzipThreads(students, f'Unzipping {len(students)}...')
        student_threads.start_thread_per_student()
        student_threads.wait_for_all_threads()

    def put_dir(self, dir):
        if not dir:
            return
        dir = os.path.realpath(dir)
        progress_bar = ProgressBar('Zipping...')
        zip_file = os.path.join(WD, dir + '.zip')
        zip_dir(zip_file, dir, progress_bar.on_progress, os.path.dirname(dir))
        students = self.students.copy()
        student2file = dict([(student, zip_file) for student in students])
        self.put(student2file, students)
        self.unzip(student2file, students)
        os.unlink(zip_file)

teacher_client = None

def start_teacher_client():
    global student_server
    global teacher_client

    student_server.stop()
    student_server = None
    teacher_client = TeacherClient()

import subprocess
import pygame.gfxdraw
import math
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

replay_log = None
if len(sys.argv) > 1 and sys.argv[1] == 'replay':
    print('replaying events from log')
    replay_log = parse_replay_log()
    screen = pg.display.set_mode(replay_log.screen_dimensions(), pg.RESIZABLE)
else:
    #screen = pygame.display.set_mode((800, 350*2), pygame.RESIZABLE)
    #screen = pygame.display.set_mode((350, 800), pygame.RESIZABLE)
    #screen = pygame.display.set_mode((1200, 350), pygame.RESIZABLE)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)

log_event(dict(STARTING=True, width=screen.get_width(), height=screen.get_height()))

screen.fill(BACKGROUND)
pygame.display.flip()
pygame.display.set_caption("Tinymation")

font = pygame.font.Font(size=screen.get_height()//15)

FADING_RATE = 3
UNDRAWABLE = (220, 215, 190)
MARGIN = (220-80, 215-80, 190-80, 192)
SELECTED = (220-80, 215-80, 190-80)
UNUSED = SELECTED
PROGRESS = (192-45, 255-25, 192-45)
LAYERS_BELOW = (128,192,255)
LAYERS_ABOVE = (255,192,0)
WIDTH = 3 # the smallest width where you always have a pure pen color rendered along
# the line path, making our naive flood fill work well...
MEDIUM_ERASER_WIDTH = 5*WIDTH
BIG_ERASER_WIDTH = 20*WIDTH
PAINT_BUCKET_WIDTH = 3*WIDTH
CURSOR_SIZE = int(screen.get_width() * 0.055)
MAX_HISTORY_BYTE_SIZE = 1*1024**3
MAX_CACHE_BYTE_SIZE = 1*1024**3
MAX_CACHED_ITEMS = 2000

print('clips read from, and saved to',WD)

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
            history = ' '.join([str(round(scale*h)) for h in reversed(self.history)])
            return f'{self.name}: {round(scale*self.total/self.calls)} ms [{round(scale*self.min)}, {round(scale*self.max)}] {history} in {self.calls} calls'
        elif self.calls==1:
            return f'{self.name}: {round(scale*self.total)} ms'
        else:
            return f'{self.name}: never called'
    def __enter__(self):
        self.start()
    def __exit__(self, *args):
        self.stop()

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
draw_bottom_timer = timers.add('bottom layers', indent=2)
draw_curr_timer = timers.add('current layer', indent=2)
draw_top_timer = timers.add('top layers', indent=2)
draw_light_timer = timers.add('light table mask', indent=2)
draw_fading_timer = timers.add('fading mask', indent=2)
draw_blits_timer = timers.add('blit surfaces', indent=2)
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
paint_bucket_timer = timers.add('PaintBucketTool.fill')
bucket_points_near_line_timer = timers.add('integer_points_near_line_segment', indent=1)
bucket_flood_fill_timer = timers.add('flood_fill_color_based_on_lines', indent=1)
timeline_down_timer = timers.add('TimelineArea.on_mouse_down')
timeline_move_timer = timers.add('TimelineArea.on_mouse_move')
flashlight_timer = timers.add('Flashlight')
mask_timer = timers.add('pen_mask',indent=1)
ff_timer = timers.add('flood_fill',indent=1)
sk_timer = timers.add('skeletonize',indent=1)
dist_timer = timers.add('distance',indent=1)
hole_timer = timers.add('patch_hole',indent=1)
rest_timer = timers.add('rest',indent=1)
thumb_timer = timers.add('thumbnail')
small_roi_timer = timers.add('small roi',indent=1)
large_roi_timer = timers.add('large roi',indent=1)

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

def greyscale_c_params(grey, is_alpha=True, expected_xstride=1):
    width, height = grey.shape
    xstride, ystride = grey.strides
    assert (xstride == 4 and is_alpha) or (xstride == expected_xstride and not is_alpha), f'xstride={xstride} is_alpha={is_alpha}'
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

tinylib.brush_init_paint.argtypes = [ctypes.c_double]*5 + [ctypes.c_int, ctypes.c_void_p] + [ctypes.c_int]*4
tinylib.brush_init_paint.restype = ctypes.c_void_p
tinylib.brush_paint.argtypes = [ctypes.c_void_p]*3 + [ctypes.c_double]*2
tinylib.brush_end_paint.argtypes = [ctypes.c_void_p]
tinylib.brush_flood_fill_color_based_on_mask.argtypes = [ctypes.c_void_p]*3 + [ctypes.c_int]*5

def rgba_array(surface):
    ptr, ystride, width, height, bgr = color_c_params(pg.surfarray.pixels3d(surface))
    buffer = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint8 * (width * ystride * 4))).contents
    return np.ndarray((height,width,4), dtype=np.uint8, buffer=buffer, strides=(ystride, 4, 1)), bgr

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

def bspline_interp(points, suggest_options, existing_lines, zoom):
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

    smoothing = len(x) / (2*zoom)
    tck, u = splprep([x, y], s=smoothing)
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
        tck, u = splprep([x, y], s=smoothing, per=True)
        add_result(tck, u[0], u[-1])
        return reversed(results)

    fit_curve_timer.stop()
    return results

def plotLines(points, ax, width, pwidth, suggest_options, existing_lines, image_width, image_height, filter_points, zoom):
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
        for path in bspline_interp(points, suggest_options, existing_lines, zoom):
            px, py = filter_points(path[0], path[1])
            add_results(px, py)
    except:
        px = np.array([x for x,y in points])
        py = np.array([y for x,y in points])
        add_results(px, py)

    return results

def drawLines(image_height, image_width, points, width, suggest_options, existing_lines, zoom, filter_points=lambda px, py: (px, py)):
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

    return plotLines(points, ax, width, pwidth, suggest_options, existing_lines, image_width, image_height, filter_points, zoom)

def drawCircle( screen, x, y, color, width):
    pygame.draw.circle( screen, color, ( x, y ), width/2 )

def drawLine(screen, pos1, pos2, color, width):
    pygame.draw.line(screen, color, pos1, pos2, width)

def make_surface(width, height):
    return pg.Surface((width, height), screen.get_flags(), screen.get_bitsize(), screen.get_masks())

import cv2
def cv2_resize_surface(src, dst):
    iptr, istride, iwidth, iheight, ibgr = color_c_params(pg.surfarray.pixels3d(src))
    optr, ostride, owidth, oheight, obgr = color_c_params(pg.surfarray.pixels3d(dst))
    assert ibgr == obgr

    ibuffer = ctypes.cast(iptr, ctypes.POINTER(ctypes.c_uint8 * (iheight * istride * 4))).contents

    # reinterpret the array as RGBA height x width (this "transposes" the image and flips R and B channels,
    # in order to fit the data into the layout cv2 expects)
    iattached = np.ndarray((iheight,iwidth,4), dtype=np.uint8, buffer=ibuffer, strides=(istride, 4, 1))

    obuffer = ctypes.cast(optr, ctypes.POINTER(ctypes.c_uint8 * (oheight * ostride * 4))).contents

    oattached = np.ndarray((oheight,owidth,4), dtype=np.uint8, buffer=obuffer, strides=(ostride, 4, 1))

    if owidth < iwidth/2:
        method = cv2.INTER_AREA
    elif owidth > iwidth:
        method = cv2.INTER_CUBIC
    else:
        method = cv2.INTER_LINEAR
    cv2.resize(iattached, (owidth,oheight), oattached, interpolation=method)

def scale_image(surface, width=None, height=None):
    assert width or height
    if not height:
        height = int(surface.get_height() * width / surface.get_width())
    if not width:
        width = int(surface.get_width() * height / surface.get_height())

    if width < surface.get_width()//2 and height < surface.get_height()//2:
        return scale_image(scale_image(surface, surface.get_width()//2, surface.get_height()//2), width, height)

    ret = pg.Surface((width, height), pg.SRCALPHA)
    cv2_resize_surface(surface, ret)
    ret.set_alpha(surface.get_alpha())
    #ret = pg.transform.smoothscale(surface, (width, height))

    return ret

def minmax(v, minv, maxv):
    return min(maxv,max(minv,v))

def load_cursor(file, flip=False, size=CURSOR_SIZE, hot_spot=(0,1), min_alpha=192, edit=lambda x: x, hot_spot_offset=(0,0)):
  surface = load_image(file)
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
pencil_cursor = (pencil_cursor[0], load_image('pen-tool.png'))
eraser_cursor = load_cursor('eraser.png')
eraser_cursor = (eraser_cursor[0], load_image('eraser-tool.png'))
eraser_medium_cursor = load_cursor('eraser.png', size=int(CURSOR_SIZE*1.5), edit=lambda s: add_circle(s, MEDIUM_ERASER_WIDTH//2), hot_spot_offset=(MEDIUM_ERASER_WIDTH//2,-MEDIUM_ERASER_WIDTH//2))
eraser_medium_cursor = (eraser_medium_cursor[0], eraser_cursor[1])
eraser_big_cursor = load_cursor('eraser.png', size=int(CURSOR_SIZE*2), edit=lambda s: add_circle(s, BIG_ERASER_WIDTH//2), hot_spot_offset=(BIG_ERASER_WIDTH//2,-BIG_ERASER_WIDTH//2))
eraser_big_cursor = (eraser_big_cursor[0], eraser_cursor[1])
flashlight_cursor = load_cursor('flashlight.png')
flashlight_cursor = (flashlight_cursor[0], load_image('flashlight-tool.png')) 
paint_bucket_cursor = (load_cursor('paint_bucket.png')[1], load_image('bucket-tool.png'))
blank_page_cursor = load_cursor('sheets.png', hot_spot=(0.5, 0.5))
garbage_bin_cursor = load_cursor('garbage.png', hot_spot=(0.5, 0.5))
needle_cursor = load_cursor('needle.png', size=int(CURSOR_SIZE*2))
zoom_cursor = load_cursor('zoom.png', hot_spot=(0.75, 0.5), size=int(CURSOR_SIZE*2))
pan_cursor = load_cursor('pan.png', hot_spot=(0.5, 0.5), size=int(CURSOR_SIZE*2))
finger_cursor = load_cursor('finger.png', hot_spot=(0.85, 0.17))

# for locked screen
empty_cursor = pg.cursors.Cursor((0,0), pg.Surface((10,10), pg.SRCALPHA))

# set_cursor can fail on some machines so we don't count on it to work.
# we set it early on to "give a sign of life" while the window is black;
# we reset it again before entering the event loop.
# if the cursors cannot be set the selected tool can still be inferred by
# the darker background of the tool selection button.
prev_cursor = None
curr_cursor = None
def try_set_cursor(c):
    try:
        global curr_cursor
        global prev_cursor
        pg.mouse.set_cursor(c)
        prev_cursor = curr_cursor
        curr_cursor = c
    except:
        pass
try_set_cursor(pencil_cursor[0])

def restore_cursor():
    try_set_cursor(prev_cursor)

def bounding_rectangle_of_a_boolean_mask(mask):
    # Sum along the vertical and horizontal axes
    vertical_sum = np.sum(mask, axis=1)
    if not np.any(vertical_sum):
        return None
    horizontal_sum = np.sum(mask, axis=0)

    minx, maxx = np.where(vertical_sum)[0][[0, -1]]
    miny, maxy = np.where(horizontal_sum)[0][[0, -1]]

    return minx, maxx, miny, maxy

class HistoryItemBase:
    def __init__(self, restore_pos_before_undo=True):
        self.restore_pos_before_undo = restore_pos_before_undo
        self.pos_before_undo = movie.pos
        self.layer_pos_before_undo = movie.layer_pos
        self.zoom_before_undo = layout.drawing_area().get_zoom_pan_params()
    def is_drawing_change(self): return False
    def from_curr_pos(self): return self.pos_before_undo == movie.pos and self.layer_pos_before_undo == movie.layer_pos
    def byte_size(history_item): return 128
    def nop(history_item): return False
    def make_undone_changes_visible(self):
        if not self.restore_pos_before_undo:
            return
        da = layout.drawing_area()
        if movie.pos != self.pos_before_undo or movie.layer_pos != self.layer_pos_before_undo or da.get_zoom_pan_params() != self.zoom_before_undo:
            movie.seek_frame_and_layer(self.pos_before_undo, self.layer_pos_before_undo)
            da.restore_zoom_pan_params(self.zoom_before_undo)
            return True

class HistoryItem(HistoryItemBase):
    def __init__(self, surface_id, bbox=None):
        HistoryItemBase.__init__(self)
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
        self.optimized = False

        if bbox:
            self.saved_alpha = self.saved_alpha[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
            if self.saved_rgb is not None:
                self.saved_rgb = self.saved_rgb[self.minx:self.maxx+1, self.miny:self.maxy+1].copy()
            self.optimized = True

    def is_drawing_change(self): return True
    def curr_surface(self):
        return movie.edit_curr_frame().surf_by_id(self.surface_id)
    def nop(self):
        return self.saved_alpha is None
    def undo(self):
        if self.nop():
            return

        if self.pos_before_undo != movie.pos or self.layer_pos_before_undo != movie.layer_pos:
            print(f'WARNING: HistoryItem at the wrong position! should be {self.pos_before_undo} [layer {self.layer_pos_before_undo}], but is {movie.pos} [layer {movie.layer_pos}]')
        movie.seek_frame_and_layer(self.pos_before_undo, self.layer_pos_before_undo) # we should already be here, but just in case (undoing in the wrong frame is a very unfortunate bug...)

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
    def optimize(self, bbox=None):
        if self.optimized:
            return

        if bbox:
            self.minx, self.miny, self.maxx, self.maxy = bbox
        else:
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

class HistoryItemSet(HistoryItemBase):
    def __init__(self, items):
        HistoryItemBase.__init__(self)
        self.items = [item for item in items if item is not None]
    def is_drawing_change(self):
        for item in self.items:
            if not item.is_drawing_change():
                return False
        return True
    def nop(self):
        for item in self.items:
            if not item.nop():
                return False
        return True
    def undo(self):
        return HistoryItemSet(list(reversed([item.undo() for item in self.items])))
    def optimize(self, bbox=None):
        for item in self.items:
            item.optimize(bbox)
        self.items = [item for item in self.items if not item.nop()]
    def byte_size(self):
        return sum([item.byte_size() for item in self.items])
    def make_undone_changes_visible(self):
        for item in self.items:
            if item.make_undone_changes_visible():
                return True

def scale_and_preserve_aspect_ratio(w, h, width, height):
    if width/height > w/h:
        scaled_width = w*height/h
        scaled_height = h*scaled_width/w
    else:
        scaled_height = h*width/w
        scaled_width = w*scaled_height/h
    return round(scaled_width), round(scaled_height)

class LayoutElemBase:
    def __init__(self): self.redraw = True
    def init(self): pass
    def hit(self, x, y): return True
    def draw(self): pass
    def on_mouse_down(self, x, y): pass
    def on_mouse_move(self, x, y): pass
    def on_mouse_up(self, x, y): pass
    def on_painting_timer(self): pass
    def on_history_timer(self): pass

class Button(LayoutElemBase):
    def __init__(self):
        LayoutElemBase.__init__(self)
        self.button_surface = None
        self.only_hit_non_transparent = False
    def draw(self, rect, cursor_surface):
        left, bottom, width, height = rect
        _, _, w, h = cursor_surface.get_rect()
        scaled_width, scaled_height = scale_and_preserve_aspect_ratio(w, h, width, height)
        if not self.button_surface:
            surface = scale_image(cursor_surface, scaled_width, scaled_height)
            self.button_surface = surface
        self.screen_left = int(left+(width-scaled_width)/2)
        self.screen_bottom = int(bottom+height-scaled_height)
        screen.blit(self.button_surface, (self.screen_left, self.screen_bottom))
    def hit(self, x, y, rect=None):
        if not self.only_hit_non_transparent:
            return True
        if rect is None:
            rect = self.rect
        if not self.button_surface:
            return False
        left, bottom, width, height = rect
        try:
            alpha = self.button_surface.get_at((x-self.screen_left, y-self.screen_bottom))[3]
        except:
            return False
        return alpha > 0

locked_image = load_image('locked.png')
invisible_image = load_image('eye_shut.png')
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
        self.rect = np.zeros(4, dtype=np.int32)
        self.region = arr_base_ptr(self.rect)
        self.bbox = None
        self.history_time_period = 1000 if eraser else 500

    def brush_flood_fill_color_based_on_mask(self):
        mask_ptr, mask_stride, width, height = greyscale_c_params(self.pen_mask, is_alpha=False)
        flood_code = 2

        color = pg.surfarray.pixels3d(movie.edit_curr_frame().surf_by_id('color'))
        color_ptr, color_stride, color_width, color_height, bgr = color_c_params(color)
        assert color_width == width and color_height == height
        new_color_value = make_color_int(self.bucket_color if self.bucket_color else (0,0,0,0), bgr)

        tinylib.brush_flood_fill_color_based_on_mask(self.brush, color_ptr, mask_ptr, color_stride, mask_stride, 0, flood_code, new_color_value)

    def on_mouse_down(self, x, y):
        if curr_layer_locked():
            return
        pen_down_timer.start()
        self.points = []
        self.bucket_color = None
        self.lines_array = pg.surfarray.pixels_alpha(movie.edit_curr_frame().surf_by_id('lines'))
        self.pen_mask = self.lines_array == 255

        drawing_area = layout.drawing_area()
        cx, cy = drawing_area.xy2frame(x, y)
        ptr, ystride, width, height = greyscale_c_params(self.lines_array)
        smoothDist = 40
        lineWidth = 2.5 if self.width == WIDTH else self.width*drawing_area.xscale
        self.brush = tinylib.brush_init_paint(cx, cy, time.time_ns()*1000000, lineWidth, smoothDist, 1 if self.eraser else 0, ptr, width, height, 4, ystride)
        if self.eraser:
            self.brush_flood_fill_color_based_on_mask()

        self.new_history_item()

        self.prev_drawn = (x,y) # Krita feeds the first x,y twice - in init-paint and in paint, here we do, too
        self.on_mouse_move(x,y)
        pg.time.set_timer(HISTORY_TIMER_EVENT, self.history_time_period, 1)
        pen_down_timer.stop()

    def new_history_item(self):
        self.bbox = (1000000, 1000000, -1, -1)
        self.lines_history_item = HistoryItem('lines')
        self.color_history_item = HistoryItem('color')

    def update_bbox(self):
        xmin, ymin, xmax, ymax = self.bbox
        rxmin, rymin, rxmax, rymax = self.rect
        self.bbox = (min(xmin, rxmin), min(ymin, rymin), max(xmax, rxmax), max(ymax, rymax))

    def on_mouse_up(self, x, y):
        if curr_layer_locked():
            return
        pen_up_timer.start()

        pg.time.set_timer(PAINTING_TIMER_EVENT, 0, 0)
        pg.time.set_timer(HISTORY_TIMER_EVENT, 0, 0)

        tinylib.brush_end_paint(self.brush, self.region)
        self.update_bbox()
        self.brush = 0
        self.prev_drawn = None

        self.save_history_item()

        self.lines_array = None

        pen_up_timer.stop()

    def save_history_item(self):
        if self.bbox[-1] >= 0:
            history_item = HistoryItemSet([self.lines_history_item, self.color_history_item])
            history_item.optimize(self.bbox)
            history.append_item(history_item)

    def on_history_timer(self):
        self.save_history_item()
        self.new_history_item()
        pg.time.set_timer(HISTORY_TIMER_EVENT, self.history_time_period, 1)

    def on_painting_timer(self):
        if self.prev_drawn:
            self.on_mouse_move(*self.prev_drawn,from_timer=True)

    def on_mouse_move(self, x, y, from_timer=False):
        if curr_layer_locked():
            return

        pen_move_timer.start()
        drawing_area = layout.drawing_area()
        cx, cy = drawing_area.xy2frame(x, y)

        # no idea why this happens for fast pen motions, but it's been known to happen - we see the first coordinate repeated for some reason
        # note that sometimes you get something close but not quite equal to the first coordinate and it's clearly wrong because it's an outlier
        # relatively to the rest of the points; not sure if we should try to second-guess the input device enough to handle it...
        if len(self.points) < 6 and (cx, cy) in self.points:
            pen_move_timer.stop()
            return

        if not from_timer:
            # if we get no mouse-move events in the next 20 ms, it means the pen stopped; since we're smoothing the pen
            # points by averaging with past points, the line will have stopped before we reach the cursor. this timer will
            # call on_mouse_move with from_timer=True and we'll then keep "hammering" the last point until the line
            # gets close enough to that point.
            #
            # TODO: in Krita things work differently; there seems to be a 7ms timer repeating the point if no new event
            # is found but seemingly _not_ to address the issue above [then for what? airbrushing?..] - with a timer like
            # that "hammering" the point you'd see the line approaching the cursor, the larger the smoothing distance the
            # more time it would take - we can implement a 7ms timer here and see the effect, and this also happens with OpenToonz
            # smoothing brushes, but not in Krita. How do things work in Krita then?..
            pg.time.set_timer(PAINTING_TIMER_EVENT, 20, 1)

        if self.eraser and self.bucket_color is None:
            nx, ny = round(cx), round(cy)
            if nx>=0 and ny>=0 and nx<self.lines_array.shape[0] and ny<self.lines_array.shape[1] and self.lines_array[nx,ny] == 0:
                self.bucket_color = movie.edit_curr_frame().surf_by_id('color').get_at((cx,cy))
                self.brush_flood_fill_color_based_on_mask()
        self.points.append((cx,cy))

        if self.prev_drawn:
            close_enough = False
            while not close_enough:
                xarr = np.array([cx])
                yarr = np.array([cy])
                tinylib.brush_paint(self.brush, arr_base_ptr(xarr), arr_base_ptr(yarr), time.time_ns()*1000000, drawing_area.xscale, self.region)
                self.update_bbox()
                if from_timer:
                    # "keep hammering the point or stop?"
                    dx = xarr[0] - cx
                    dy = yarr[0] - cy
                    close_enough = math.sqrt(dx*dx + dy*dy) < 1
                    if pg.event.peek(pg.MOUSEMOTION):
                        break
                else:
                    break
            
        #self.lines_array[round(cx),round(cy)] = 255
        if False and expose_other_layers:
            roi, (xstart, ystart, iwidth, iheight) = drawing_area.frame_and_subsurface_roi()
            # FIXME we get an exception here sometimes [in subsurface() - rectangle outside surface area]
            draw_into = drawing_area.subsurface if not expose_other_layers else self.alpha_surface.subsurface((xstart, ystart, iwidth, iheight))
            ox,oy = (0,0) if not expose_other_layers else (xstart, ystart)

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

            render_surface(movie.curr_bottom_layers_surface(movie.pos, highlight=True, width=iwidth, height=iheight, roi=roi))
            render_surface(movie.curr_top_layers_surface(movie.pos, highlight=True, width=iwidth, height=iheight, roi=roi))
            render_surface(layout.timeline_area().combined_light_table_mask())

        self.prev_drawn = (x,y) 
        pen_move_timer.stop()

MIN_ZOOM, MAX_ZOOM = 1, 5

class ZoomTool(Button):
    def on_mouse_down(self, x, y):
        self.start = (x,y)
        da = layout.drawing_area()
        abs_y = y + da.rect[1]
        h = screen.get_height()
        self.max_up_dist = min(.85 * abs_y, h * .3 * (MAX_ZOOM - da.zoom)/(MAX_ZOOM - MIN_ZOOM))
        self.max_down_dist = min(.85 * (h - abs_y), h * .3 * (da.zoom - MIN_ZOOM)/(MAX_ZOOM - MIN_ZOOM))
        self.frame_start = da.xy2frame(x,y,minoft=-1000000)
        self.orig_zoom = da.zoom
        da.set_zoom_center(self.start)
    def on_mouse_up(self, x, y):
        layout.drawing_area().draw()
    def on_mouse_move(self, x, y):
        px, py = self.start
        up = y < py
        da = layout.drawing_area()
        if up and self.max_up_dist == 0:
            new_zoom = MAX_ZOOM
            ratio = 0
        elif not up and self.max_down_dist == 0:
            new_zoom = MIN_ZOOM
            ratio = 1
        else:
            dist = abs(py - y) #math.sqrt(sqdist((x,y), (px,py)))#abs(y - py)
            ratio = min(1, max(0, dist/(self.max_up_dist if up else self.max_down_dist)))
            zoom_change = ratio*((MAX_ZOOM - self.orig_zoom) if up else (self.orig_zoom - MIN_ZOOM))
            if not up:
                zoom_change = -zoom_change
            new_zoom = max(MIN_ZOOM,min(self.orig_zoom + zoom_change, MAX_ZOOM))
        da.set_zoom(new_zoom)

        # we want xy2frame(self.start) to return the same value at the beginnig of the zooming [if possible]
        # we then want xy2frame(iwidth/2, iheight/2) to eventually converge to self.frame_start [if possible]
        # centerx, centery is somewhere between these two "x/yoffset-defining" points
        centerx = (da.iwidth/2)*ratio + px*(1-ratio)
        centery = (da.iheight/2)*ratio + py*(1-ratio)
        framex, framey = self.frame_start
        xoffset = framex/da.xscale - centerx + da.xmargin
        yoffset = framey/da.yscale - centery + da.ymargin
        da.set_xyoffset(xoffset, yoffset)

        da.set_zoom_center(da.frame2xy(*self.frame_start))

class NewDeleteTool(PenTool):
    def __init__(self, frame_func, clip_func, layer_func):
        PenTool.__init__(self)
        self.frame_func = frame_func
        self.clip_func = clip_func
        self.layer_func = layer_func

    def on_mouse_down(self, x, y): pass
    def on_mouse_up(self, x, y): pass
    def on_mouse_move(self, x, y): pass

def flood_fill_color_based_on_lines(color_rgba, lines, x, y, bucket_color, bbox_callback=None):
    flood_code = 2
    global pen_mask
    pen_mask = lines==255

    rect = np.zeros(4, dtype=np.int32)
    region = arr_base_ptr(rect)
    mask_ptr, mask_stride, width, height = greyscale_c_params(pen_mask, is_alpha=False)
    assert x >= 0 and x < width and y >= 0 and y < height
    # TODO: if we use OpenCV anyway for resizing, maybe use floodfill from there?..
    tinylib.flood_fill_mask(mask_ptr, mask_stride, width, height, x, y, flood_code, region, 0)
    
    xstart, ystart, xlen, ylen = rect
    bbox_retval = (xstart, ystart, xstart+xlen-1, ystart+ylen-1)
    if bbox_callback:
        bbox_retval = bbox_callback(bbox_retval)

    color_ptr, color_stride, color_width, color_height, bgr = color_c_params(color_rgba)
    assert color_width == width and color_height == height
    new_color_value = make_color_int(bucket_color, bgr)
    tinylib.fill_color_based_on_mask(color_ptr, mask_ptr, color_stride, mask_stride, width, height, region, new_color_value, flood_code)

    del pen_mask
    pen_mask = None

    return bbox_retval

def flood_fill_color_based_on_mask_many_seeds(color_rgba, pen_mask, xs, ys, bucket_color):
    mask_ptr, mask_stride, width, height = greyscale_c_params(pen_mask, is_alpha=False)
    flood_code = 2

    color_ptr, color_stride, color_width, color_height, bgr = color_c_params(color_rgba)
    assert color_width == width and color_height == height
    new_color_value = make_color_int(bucket_color, bgr)

    rect = np.zeros(4, dtype=np.int32)
    region = arr_base_ptr(rect)

    assert len(xs) == len(ys)
    assert xs.strides == (4,)
    assert ys.strides == (4,)
    x_ptr = arr_base_ptr(xs)
    y_ptr = arr_base_ptr(ys)

    tinylib.flood_fill_color_based_on_mask_many_seeds(color_ptr, mask_ptr, color_stride, mask_stride,
        width, height, region, 0, flood_code, new_color_value, x_ptr, y_ptr, len(xs))
    xmin, ymin, xmax, ymax = rect
    if xmax >= 0 and ymax >= 0:
        return xmin, ymin, xmax-1, ymax-1

def point_line_distance_vectorized(px, py, x1, y1, x2, y2):
    """ Vectorized calculation of distance from multiple points (px, py) to the line segment (x1, y1)-(x2, y2) """
    line_mag = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    if line_mag < 1e-8:
        # The line segment is a point
        return np.sqrt((px - x1) ** 2 + (py - y1) ** 2)
    
    # Projection of points on the line segment
    u = ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / (line_mag ** 2)
    u = np.clip(u, 0, 1)  # Clamping the projection
    
    # Coordinates of the projection points
    ix = x1 + u * (x2 - x1)
    iy = y1 + u * (y2 - y1)
    
    # Distance from points to the projection points
    return np.sqrt((px - ix) ** 2 + (py - iy) ** 2)

def integer_points_near_line_segment(x1, y1, x2, y2, distance):
    """ Vectorized find all integer coordinates within a given distance from the line segment (x1, y1)-(x2, y2) """
    # Determine the bounding box
    xmin = np.floor(min(x1, x2) - distance)
    xmax = np.ceil(max(x1, x2) + distance)
    ymin = np.floor(min(y1, y2) - distance)
    ymax = np.ceil(max(y1, y2) + distance)
    
    # Generate grid of integer points within the bounding box
    x = np.arange(xmin, xmax + 1)
    y = np.arange(ymin, ymax + 1)
    xx, yy = np.meshgrid(x, y)
    
    # Flatten the grids to get coordinates
    px, py = xx.ravel(), yy.ravel()
    
    # Compute distances using vectorized function
    distances = point_line_distance_vectorized(px, py, x1, y1, x2, y2)
    
    # Filter points within the specified distance
    mask = distances <= distance
    result_points = np.vstack((px[mask], py[mask])).T
    
    return result_points.astype(np.int32)

class PaintBucketTool(Button):
    def __init__(self,color):
        Button.__init__(self)
        self.color = color
        self.px = None
        self.py = None
        self.bboxes = []
        self.pen_mask = None
    def fill(self, x, y):
        if curr_layer_locked():
            return
        paint_bucket_timer.start()

        x, y = layout.drawing_area().xy2frame(x,y)
        x, y = round(x), round(y)

        if self.px is None:
            self.px = x
            self.py = y

        radius = (PAINT_BUCKET_WIDTH//2) * layout.drawing_area().xscale
        with bucket_points_near_line_timer:
            points = integer_points_near_line_segment(self.px, self.py, x, y, radius)
            xs = points[:,0]
            ys = points[:,1]
        self.px = x
        self.py = y
        
        with bucket_flood_fill_timer:
            color_rgba = pg.surfarray.pixels3d(movie.edit_curr_frame().surf_by_id('color'))
            bbox = flood_fill_color_based_on_mask_many_seeds(color_rgba, self.pen_mask, xs, ys, self.color)
        if bbox:
            self.bboxes.append(bbox)

        # not redrawing - using the "is_pressed" workaround PenTool uses, too until we learn to redraw only a part of the region
        # TODO: only redraw within the bbox?
        #layout.drawing_area().draw()
        
        paint_bucket_timer.stop()

    def on_mouse_down(self, x, y):
        self.history_item = HistoryItem('color')
        self.bboxes = []
        self.px = None
        self.py = None
        lines = pg.surfarray.pixels_alpha(movie.curr_frame().surf_by_id('lines'))
        self.pen_mask = lines == 255

        self.fill(x,y)
    def on_mouse_move(self, x, y):
        if self.pen_mask is None: # pen_mask is None has been known to happen in flood_fill_color_based_on_mask_many_seeds...
            self.on_mouse_down(x,y)
        else:
            self.fill(x,y)
    def on_mouse_up(self, x, y):
        self.on_mouse_move(x,y)
        if self.bboxes: # we had changes
            inf = 10**9
            minx, miny, maxx, maxy = inf, inf, -inf, -inf
            for iminx, iminy, imaxx, imaxy in self.bboxes:
                minx = min(iminx, minx)
                miny = min(iminy, miny)
                maxx = max(imaxx, maxx)
                maxy = max(imaxy, maxy)
            self.history_item.optimize((minx, miny, maxx, maxy))
            history.append_item(self.history_item)
        self.history_item = None
        self.pen_mask = None

NO_PATH_DIST = 10**6

def skeleton_to_distances(skeleton, x, y):
    dist = np.zeros(skeleton.shape, np.float32)

    sk_ptr, sk_stride, _, _ = greyscale_c_params(skeleton.T, is_alpha=False)
    dist_ptr, dist_stride, width, height = greyscale_c_params(dist.T, expected_xstride=4, is_alpha=False)
    
    maxdist = tinylib.image_dijkstra(sk_ptr, sk_stride, dist_ptr, dist_stride//4, width, height, y, x)

    return dist, maxdist

import colorsys

def tl_skeletonize(mask):
    skeleton = np.zeros(mask.shape,np.uint8)
    mask_ptr, mask_stride, width, height = greyscale_c_params(mask.T, is_alpha=False)
    sk_ptr, sk_stride, _, _ = greyscale_c_params(skeleton.T, is_alpha=False)
    tinylib.skeletonize(mask_ptr, mask_stride, sk_ptr, sk_stride, width, height)
    return skeleton

def fixed_size_region_1d(center, part, full): 
    assert part*2 <= full
    if center < part//2:
        start = 0
    elif center > full - part//2:
        start = full - part
    else:
        start = center - part//2
    return slice(start, start+part)

def fixed_size_image_region(x, y, w, h):
    xs = fixed_size_region_1d(x, w, IWIDTH)
    ys = fixed_size_region_1d(y, h, IHEIGHT)
    return xs, ys

SK_WIDTH = 350
SK_HEIGHT = 350

# we use a 4-connectivity flood fill so the following 4-pixel pattern is "not a hole":
#
# 0 1
# 1 0
#
# however skeletonize considers this a connected component, so we detect and close such "holes."
# this shouldn't result in closing a 4-connectivity hole like this:
#
# 0 1 0
# 0 0 0
# 1 1 1
#
# since whatever the x values, the middle 0 cannot be closed, only the zeros around it,
# which still leaves a "4-connected hole" that skeletonize will treat as a hole
def close_diagonal_holes(mask):
    diag1 = mask[1:,:-1] & mask[:-1,1:] & ~mask[1:,1:]
    diag2 = mask[:-1,:-1] & mask[1:,1:] & ~mask[:-1,1:]

    # FIXME: handle the full range
    mask[:-1,:-1] |= diag1
    mask[1:,:-1] |= diag2 

def skeletonize_color_based_on_lines(color, lines, x, y):
    mask_timer.start()
    pen_mask = lines == 255
    mask_timer.stop()
    if pen_mask[x,y]:
        flashlight_timer.stop()
        return

    close_diagonal_holes(pen_mask)

    ff_timer.start()
    flood_code = 2
    flood_mask = np.ascontiguousarray(pen_mask.astype(np.uint8))
    cv2.floodFill(flood_mask, None, seedPoint=(y, x), newVal=flood_code, loDiff=(0, 0, 0, 0), upDiff=(0, 0, 0, 0))
    flood_mask = flood_mask == flood_code
    #flood_mask = flood_fill(pen_mask.astype(np.byte), (x,y), flood_code) == flood_code
    ff_timer.stop()
        
    sk_timer.start()
    skx, sky = fixed_size_image_region(x, y, SK_WIDTH, SK_HEIGHT)
    skeleton = skeletonize(np.ascontiguousarray(flood_mask[skx,sky])).astype(np.uint8)
    sk_timer.stop()

    fmb = binary_dilation(binary_dilation(skeleton))

    # Compute distance from each point to the specified center
    dist_timer.start()
    d, maxdist = skeleton_to_distances(skeleton, x-skx.start, y-sky.start)
    dist_timer.stop()


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

    maxdist = min(700, maxdist)

    rest_timer.start()
    inner = (255,255,255)
    outer = [255-ch for ch in color[x,y]]
    h,s,v = colorsys.rgb_to_hsv(*[o/255. for o in outer])
    s = 1
    v = 1
    outer = [255*o for o in colorsys.hsv_to_rgb(h,s,v)]

    fading_mask = pg.Surface((flood_mask.shape[0], flood_mask.shape[1]), pg.SRCALPHA)
    fm = pg.surfarray.pixels3d(fading_mask)
    for ch in range(3):
         fm[skx,sky,ch] = outer[ch]*(1-skeleton) + inner[ch]*skeleton
    pg.surfarray.pixels_alpha(fading_mask)[skx,sky] = fmb*255*np.maximum(0,pow(1 - outer_d/maxdist, 3))

    rest_timer.stop()
    return fading_mask, (skeleton, skx, sky)

HOLE_REGION_W = 40
HOLE_REGION_H = 40

def patch_hole(lines, x, y, skeleton, skx, sky):
    lines_patch = np.ascontiguousarray(lines[skx,sky])
    # pad the lines with 255 if we're near the image boundary, to patch holes near image boundaries

    def add_boundary(arr):
        new = np.zeros((arr.shape[0]+2, arr.shape[1]+2), arr.dtype)
        new[1:-1,1:-1] = arr
        return new

    lines_patch = add_boundary(lines_patch)
    skeleton = add_boundary(skeleton)
    skx = slice(skx.start-1,skx.stop+1)
    sky = slice(sky.start-1,sky.stop+1)

    if skx.start < 0:
        lines_patch[0,:] = 255
    if sky.start < 0:
        lines_patch[:,0] = 255
    if skx.stop > lines.shape[0]:
        lines_patch[-1,:] = 255
    if sky.stop > lines.shape[1]:
        lines_patch[:,-1] = 255

    sk_ptr, sk_stride, _, _ = greyscale_c_params(skeleton.T, is_alpha=False)
    lines_ptr, lines_stride, width, height = greyscale_c_params(lines_patch.T, is_alpha=False)
    
    npoints = 3
    xs = np.zeros(npoints, np.int32)
    ys = np.zeros(npoints, np.int32)

    nextra = 100
    xs1 = np.zeros(nextra, np.int32)
    ys1 = np.zeros(nextra, np.int32)
    n1 = np.array([nextra], np.int32)
    xs2 = np.zeros(nextra, np.int32)
    ys2 = np.zeros(nextra, np.int32)
    n2 = np.array([nextra], np.int32)

    # TODO: if the closest point on the skeleton is near invisible (due to the past distance computation),
    # maybe better to recompute the distances and repaint instead of going ahead and patching?..
    found = tinylib.patch_hole(lines_ptr, lines_stride, sk_ptr, sk_stride, width, height, y-sky.start, x-skx.start,
                               HOLE_REGION_H, HOLE_REGION_W, arr_base_ptr(ys), arr_base_ptr(xs), npoints,
                               arr_base_ptr(ys1), arr_base_ptr(xs1), arr_base_ptr(n1),
                               arr_base_ptr(ys2), arr_base_ptr(xs2), arr_base_ptr(n2))

    if found < 3:
        return False
    n1 = n1[0] #* 0
    n2 = n2[0] #* 0
    xs1 = xs1[:n1]
    ys1 = ys1[:n1]
    xs2 = xs2[:n2]
    ys2 = ys2[:n2]
    #skeleton[xs,ys] = 5
    #skeleton[xs1,ys1] = 6
    #skeleton[xs2,ys2] = 7
    #print('LP xs ys',lines[xs+skx.start,ys+sky.start])
    #imageio.imwrite('lines-skel.png', skeleton.astype(np.uint8)*(256//8))
    #imageio.imwrite('lines-bin.png', lines_patch.astype(np.uint8)*127)

    endp1 = xs[0]+skx.start, ys[0]+sky.start
    endp2 = xs[2]+skx.start, ys[2]+sky.start

    if n1 == 0 and n2 == 0: #  just 3 points - create 5 points to fit a curve
        # through the point on the skeleton and the 2 endpoints
        pass
        #xs = [xs[0], xs[0]*0.9 + xs[1]*0.1, xs[1], xs[1]*0.1 + xs[2]*0.9, xs[2]]
        #ys = [ys[0], ys[0]*0.9 + ys[1]*0.1, ys[1], ys[1]*0.1 + ys[2]*0.9, ys[2]]
    else: # we have enough points to not depend on the exact point
        # on the skeleton
        def pad(c, n): # a crude way to add weight to a "lone endpoint",
            # absent this the line fitting can fail to reach it
            eps = 0.0001
            return [c+eps*i for i in range(1 + 9*(n==0))]
        xs = pad(xs[0],n1) + pad(xs[2],n2)
        ys = pad(ys[0],n1) + pad(ys[2],n2)


    # +.5 because line skeletonization done inside tinylib.patch_hole
    # seems to move "the line center of mass" by about half a pixel
    #print(xs1[::-1]+.5, xs, xs2+.5)
    #print(ys1[::-1]+.5, ys, ys2+.5)
    xs = np.concatenate((xs1[::-1]+.5, xs, xs2+.5))
    ys = np.concatenate((ys1[::-1]+.5, ys, ys2+.5))
    lines_patch[:] = 0
    skeleton[:] = 0
    points=[(x+skx.start,y+sky.start) for x,y in zip(xs,ys)]

    def filter_points(px, py):
        start = 0
        end = -1
        for i in range(len(px)):
            if not start and (px[i] - endp1[0])**2 + (py[i] - endp1[1])**2 < 4:
                start = i
                break
        for i in reversed(range(len(px))):
            if end < 0 and (px[i] - endp2[0])**2 + (py[i] - endp2[1])**2 < 4:
                end = i
                break
        return px[start:end], py[start:end]
    # TODO: use the brush instead
    history_item = HistoryItem('lines') # TODO: pass bbox

    ptr, ystride, width, height = greyscale_c_params(lines)
    brush = tinylib.brush_init_paint(points[0][0], points[1][1], 0, 2.5, 0, 0, ptr, width, height, 4, ystride)
    xarr = np.zeros(1)
    yarr = np.zeros(1)
    t = 0
    for x,y in points:
        xarr[0] = x
        yarr[0] = y
        tinylib.brush_paint(brush, arr_base_ptr(xarr), arr_base_ptr(yarr), t, 1)
        t += 7

    tinylib.brush_end_paint(brush)
    
    history.append_item(history_item)

    #new_lines,bbox = drawLines(IWIDTH, IHEIGHT, points, WIDTH, False, lines, zoom=1, filter_points=filter_points)[0]

    #history_item = HistoryItem('lines', bbox)
    #(minx, miny, maxx, maxy) = bbox
    #lines[minx:maxx+1, miny:maxy+1] = np.maximum(new_lines, lines[minx:maxx+1, miny:maxy+1])
    #history.append_item(history_item)

    return True

last_skeleton = None

class FlashlightTool(Button):
    def __init__(self):
        Button.__init__(self)
    def on_mouse_down(self, x, y):
        x, y = layout.drawing_area().xy2frame(x,y)
        x, y = round(x), round(y)

        try_to_patch = pygame.key.get_mods() & pygame.KMOD_CTRL
        frame = movie.edit_curr_frame() if try_to_patch else movie.curr_frame()

        color = pygame.surfarray.pixels3d(frame.surf_by_id('color'))
        lines = pygame.surfarray.pixels_alpha(frame.surf_by_id('lines'))
        if x < 0 or y < 0 or x >= color.shape[0] or y >= color.shape[1]:
            return
        flashlight_timer.start()

        if try_to_patch:
            # Ctrl pressed - attempt to patch a hole using the previous skeleton (if relevant
            if last_skeleton:
                skeleton, skx, sky = last_skeleton
                if x >= skx.start and x < skx.stop and y >= sky.start and y < sky.stop:
                    hole_timer.start()
                    if patch_hole(lines, x, y, skeleton, skx, sky):
                        # find a point to compute a new skeleton around. Sometimes x,y itself
                        # is that point and sometimes a neighbor, depending on how the hole was patched.
                        # we want "some" sort of skeleton to give a clear feedback showing that "a hole
                        # was really patched" and the skeleton running into the patch is good feedback.
                        # if we just show no skeleton then if the patch is near invisible it's not clear
                        # what happened. the downside is that we don't know which side of the hole
                        # the new skeleton should be at; we could probably compute several and choose the
                        # largest but seems like too much trouble?..
                        neighbors = [(0,0),(2,0),(-2,0),(0,2),(0,-2),(2,2),(-2,-2),(-2,2),(2,-2)]
                        for ox,oy in neighbors:
                            xi,yi = x+ox,y+oy
                            if xi < 0 or yi < 0 or xi >= color.shape[0] or yi >= color.shape[1]:
                                continue
                            if lines[xi,yi] != 255:
                                break
                        x,y = xi,yi
                    hole_timer.stop()

        fading_mask_and_skeleton = skeletonize_color_based_on_lines(color, lines, x, y)

        flashlight_timer.stop()
        if not fading_mask_and_skeleton:
            return
        fading_mask, skeleton = fading_mask_and_skeleton
        fading_mask.set_alpha(255)
        layout.drawing_area().set_fading_mask(fading_mask, skeleton)
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

DRAWING_LAYOUT = 1
ANIMATION_LAYOUT = 2

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
        self.restore_tool_on_mouse_up = False
        self.mode = ANIMATION_LAYOUT

    def aspect_ratio(self): return self.width/self.height

    def add(self, rect, elem, draw_border=False):
        srect = scale_rect(rect)
        elem.rect = srect
        elem.subsurface = screen.subsurface(srect)
        elem.draw_border = draw_border
        elem.init()
        self.elems.append(elem)

    def draw_locked(self):
        screen.fill(PEN)
        screen.blit(locked_image, ((screen.get_width()-locked_image.get_width())//2, (screen.get_height()-locked_image.get_height())//2))

    def hidden(self, elem):
        return self.mode == DRAWING_LAYOUT and (isinstance(elem, TimelineArea) or isinstance(elem, LayersArea) or isinstance(elem, TogglePlaybackButton))

    def draw(self):
        if self.is_pressed:
            if self.focus_elem is self.drawing_area():
                return
            if not self.focus_elem.redraw:
                return

        layout_draw_timer.start()

        screen.fill(UNDRAWABLE)
        for elem in self.elems:
            if not self.is_playing or isinstance(elem, DrawingArea) or isinstance(elem, TogglePlaybackButton):
                if self.hidden(elem):
                    continue
                try:
                    elem.draw()
                except:
                    import traceback
                    traceback.print_exc()
                    pygame.draw.rect(screen, (255,0,0), elem.rect, 3, 3)
                    continue
                if elem.draw_border:
                    pygame.draw.rect(screen, PEN, elem.rect, 1, 1)

        self.draw_students()

        layout_draw_timer.stop()

    def draw_students(self):
        if teacher_client:
            text_surface = font.render(f"{len(teacher_client.students)} students", True, (255, 0, 0), (255, 255, 255))
            screen.blit(text_surface, ((screen.get_width()-text_surface.get_width()), (screen.get_height()-text_surface.get_height())))

    # note that pygame seems to miss mousemove events with a Wacom pen when it's not pressed.
    # (not sure if entirely consistently.) no such issue with a regular mouse
    def on_event(self,event):
        if event.type == REDRAW_LAYOUT_EVENT:
            return

        if event.type == RELOAD_MOVIE_LIST_EVENT:
            clips_were_inserted_via_filesystem()
            return

        if event.type == PLAYBACK_TIMER_EVENT:
            if self.is_playing:
                self.playing_index = (self.playing_index + 1) % len(movie.frames)
            # when zooming/panning, we redraw at the playback rate [instead of per mouse event,
            # which can create a "backlog" where we keep redrawing after the mouse stops moving because we
            # lag after mouse motion.] TODO: do we want to use a similar approach elsewhere?..
            elif self.is_pressed: # FIXME and self.zoom_pan_tool() and self.focus_elem is self.drawing_area():
                cache.lock() # the chance to need to redraw with the same intermediate zoom/pan is low
                self.drawing_area().draw()
                cache.unlock()
                pg.display.flip()

        if event.type == FADING_TIMER_EVENT:
            self.drawing_area().update_fading_mask()

        if event.type == SAVING_TIMER_EVENT:
            movie.frame(movie.pos).save()

        if event.type == PAINTING_TIMER_EVENT:
            self.tool.on_painting_timer()

        if event.type == HISTORY_TIMER_EVENT:
            self.tool.on_history_timer()

        if event.type not in [pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION]:
            return

        if event.type in [pg.MOUSEMOTION, pg.MOUSEBUTTONUP] and not self.is_pressed:
            return # this guards against processing mouse-up with a button pressed which isn't button 0,
            # as well as, hopefully, against various mysterious occurences observed in the wild where
            # we eg are drawing a line even though we aren't actually trying

        x, y = event.pos

        dispatched = False
        for elem in self.elems:
            left, bottom, width, height = elem.rect
            if x>=left and x<left+width and y>=bottom and y<bottom+height:
                if not self.is_playing or isinstance(elem, TogglePlaybackButton):
                    if self.hidden(elem):
                        continue
                    if elem.hit(x,y):
                        self._dispatch_event(elem, event, x, y)
                        dispatched = True
                        break

        if not dispatched and self.focus_elem:
            self._dispatch_event(None, event, x, y)
            return

    def _dispatch_event(self, elem, event, x, y):
        if event.type == pygame.MOUSEBUTTONDOWN:
            change = tool_change
            self.is_pressed = True
            self.focus_elem = elem
            if self.focus_elem:
                elem.on_mouse_down(x,y)
            if change == tool_change and self.new_delete_tool():
                self.restore_tool_on_mouse_up = True
        elif event.type == pygame.MOUSEBUTTONUP:
            self.is_pressed = False
            if self.restore_tool_on_mouse_up:
                restore_tool()
                self.restore_tool_on_mouse_up = False
                return
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

    def new_delete_tool(self): return isinstance(self.tool, NewDeleteTool) 
    def zoom_pan_tool(self): return isinstance(self.tool, ZoomTool)

    def toggle_playing(self):
        self.is_playing = not self.is_playing
        self.playing_index = 0
            
class DrawingArea(LayoutElemBase):
    def init(self):
        self.fading_mask = None
        self.fading_func = None
        self.fade_per_frame = 0
        self.last_update_time = 0
        self.ymargin = WIDTH * 3
        self.xmargin = WIDTH * 3
        self.render_surface = None
        self.iwidth = 0
        self.iheight = 0
        self.zoom = 1
        self.zoom_center = (0, 0)
        self.xoffset = 0
        self.yoffset = 0
        self.fading_mask_version = 0
        self.restore_tool_on_mouse_up = False
    def _internal_layout(self):
        if self.iwidth and self.iheight:
            return
        left, bottom, width, height = self.rect
        self.iwidth, self.iheight = scale_and_preserve_aspect_ratio(IWIDTH, IHEIGHT, width - self.xmargin*2, height - self.ymargin*2)
        self.xmargin = round((width - self.iwidth)/2)
        self.ymargin = round((height - self.iheight)/2)
        self.set_zoom(self.zoom)

        w, h = ((self.iwidth+self.xmargin*2 + self.iheight+self.ymargin*2)//2,)*2
        self.zoom_surface = pg.Surface((w,h ), pg.SRCALPHA)
        self.zoom_surface.fill(([(a+b)//2 for a,b in zip(MARGIN[:3], BACKGROUND[:3])]))
        rgb = pg.surfarray.pixels3d(self.zoom_surface)
        alpha = pg.surfarray.pixels_alpha(self.zoom_surface)
        yv, xv = np.meshgrid(np.arange(h), np.arange(w))
        cx, cy = w/2, h/2
        dist = np.sqrt((xv-cx)**2 + (yv-cy)**2)
        mdist = np.max(dist)
        rgb[dist >= 0.7*mdist] = MARGIN[:3]
        dist = np.minimum(np.maximum(dist, mdist*0.43), mdist*0.7)
        norm_dist = np.maximum(0, dist-mdist*0.43)/(mdist*(0.7-0.43))
        grad = (1 + np.sin(30*norm_dist**1.3))/2
        for i in range(3):
            rgb[:,:,i] = (BACKGROUND[i]*grad +MARGIN[i]*(1-grad))
        alpha[:] = MARGIN[-1]*norm_dist
    def set_xyoffset(self, xoffset, yoffset):
        self._internal_layout()
        prevxo, prevyo = self.xoffset, self.yoffset
        self.xoffset = min(max(xoffset, 0), self.iwidth*(self.zoom - 1))
        self.yoffset = min(max(yoffset, 0), self.iheight*(self.zoom - 1))
        # make sure xoffset,yoffset correspond to an integer coordinate in the non-zoomed image,
        # otherwise the scaled image cannot match xoffset, yoffset exactly. NOTE: this causes "dancing"
        # when zooming - xy offset jumps... could be called a bad consequence of using off the shelf
        # scaling code that doesn't support backward warping from non-integer source coordinates...
        xm = self.xmargin * IWIDTH / self.iwidth
        ym = self.ymargin * IHEIGHT / self.iheight
        x, y = [c/self.zoom for c in (self.xoffset*IWIDTH/self.iwidth - xm, self.yoffset*IHEIGHT/self.iheight - ym)]
        self.xoffset = (math.floor(x) * self.zoom + xm) * self.iwidth / IWIDTH
        self.yoffset = (math.floor(y) * self.zoom + ym) * self.iheight / IHEIGHT

        zx, zy = self.zoom_center
        self.zoom_center = zx - (self.xoffset - prevxo), zy - (self.yoffset - prevyo)
    def set_zoom(self, zoom):
        self.zoom = zoom
        self.xscale = IWIDTH/(self.iwidth * self.zoom)
        self.yscale = IHEIGHT/(self.iheight * self.zoom)
    def set_zoom_center(self, center): self.zoom_center = center
    def set_zoom_to_film_res(self, center):
        cx, cy = center
        framex, framey = self.xy2frame(cx, cy)

        # in this class, "zoom=1" is zooming to iwidth,iheight; this is zooming to IWIDTH,IHEIGHT - what would normally be called "1x zoom"
        self.xscale = 1
        self.yscale = 1
        self.zoom = IWIDTH / self.iwidth

        # set xyoffset s.t. the center stays at the same screen location (=we zoom around the center)
        xoffset = framex + self.xmargin - cx
        yoffset = framey + self.ymargin - cy
        self.set_xyoffset(xoffset, yoffset)

        self.set_zoom_center(center)
    def frame_and_subsurface_roi(self):
        # ignoring margins, the roi in the drawing area shows the frame scaled by zoom and then cut
        # to the subsurface xoffset, yoffset, iwidth, iheight
        def trim_roi(roi, round_xy, check_round):
            left,bottom,width,height = roi
            left = max(0, left)
            bottom = max(0, bottom)
            def round_and_check(c):
                if not round_xy:
                    return c
                rc = round(c)
                if check_round:
                    assert abs(rc - c) < 0.0001, f'expecting a coordinate value very close to an integer, got {c}'
                return rc
            left = round_and_check(left)
            bottom = round_and_check(bottom)
            width = min(width, IWIDTH-left)
            height = min(height, IHEIGHT-bottom)
            return left,bottom,width,height
        no_margins_frame_roi = trim_roi([c/self.zoom for c in (self.xoffset*IWIDTH/self.iwidth, self.yoffset*IHEIGHT/self.iheight, IWIDTH, IHEIGHT)], round_xy=False, check_round=False)
        xm = self.xmargin * IWIDTH / self.iwidth
        ym = self.ymargin * IHEIGHT / self.iheight
        frame_roi = trim_roi([c/self.zoom for c in (self.xoffset*IWIDTH/self.iwidth - xm, self.yoffset*IHEIGHT/self.iheight - ym, IWIDTH+xm*2, IHEIGHT+ym*2)], round_xy=True, check_round=True)
        xstart = self.xmargin-(no_margins_frame_roi[0] - frame_roi[0])/self.xscale
        ystart = self.ymargin-(no_margins_frame_roi[1] - frame_roi[1])/self.yscale
        sub_roi = trim_roi((xstart, ystart, frame_roi[2]/self.xscale, frame_roi[3]/self.yscale), round_xy=True, check_round=False)
        return frame_roi, sub_roi
    def xy2frame(self, x, y, minoft=0):
        # we need minoft because we get small negative xoffset/yoffset upon zooming and panning to the rightmost/bottommost extent,
        # and this throws off everything except the zoom tool which seems to need it?!.. TODO: understand and solve this properly
        return (x - self.xmargin + max(minoft,self.xoffset))*self.xscale, (y - self.ymargin + max(minoft,self.yoffset))*self.yscale
    def frame2xy(self, framex, framey):
        return framex/self.xscale + self.xmargin - self.xoffset, framey/self.yscale + self.ymargin - self.yoffset
    def roi(self, surface):
        if self.zoom == 1:
            return surface
        return surface.subsurface((self.xoffset, self.yoffset, self.iwidth, self.iheight))
    def scale_and_cache(self, surface, key, get_key=False):
        self._internal_layout()
        class ScaledSurface:
            def compute_key(_):
                id2version, comp = key
                return id2version, ('scaled-to-drawing-area', comp, self.zoom, self.xoffset, self.yoffset)
            def compute_value(_):
                frame_roi, (_, _, iwidth, iheight) = self.frame_and_subsurface_roi()
                return scale_image(surface.subsurface(frame_roi), iwidth, iheight)
        if get_key:
            return ScaledSurface().compute_key()
        if surface is None:
            return None
        return cache.fetch(ScaledSurface())
    def set_fading_mask(self, fading_mask, skeleton=None):
        self.fading_mask_version += 1
        cache.update_id('fading-mask', self.fading_mask_version)
        self.fading_mask = fading_mask
        global last_skeleton
        last_skeleton = skeleton
    def scaled_fading_mask(self):
        key = (('fading-mask',self.fading_mask_version),), 'fading-mask'
        m = self.scale_and_cache(self.fading_mask, key)
        m.set_alpha(self.fading_mask.get_alpha())
        return m
    def get_zoom_pan_params(self):
        return self.zoom, self.xoffset, self.yoffset, self.zoom_center, self.xscale, self.yscale
    def restore_zoom_pan_params(self, params):
        self.zoom, self.xoffset, self.yoffset, self.zoom_center, self.xscale, self.yscale = params
    def reset_zoom_pan_params(self):
        self.set_xyoffset(0, 0)
        self.set_zoom(1)
    def draw(self):
        drawing_area_draw_timer.start()

        self._internal_layout()
        left, bottom, width, height = self.rect

        if layout.is_playing:
            zoom_params = self.get_zoom_pan_params()
            self.reset_zoom_pan_params()

        def draw_margin(margin_color):
            pygame.gfxdraw.box(self.subsurface, (0, 0, width, self.ymargin), margin_color)
            pygame.gfxdraw.box(self.subsurface, (0, self.ymargin, self.xmargin, height-self.ymargin), margin_color)
            pygame.gfxdraw.box(self.subsurface, (width-self.xmargin, self.ymargin, self.xmargin, height-self.ymargin), margin_color)
            pygame.gfxdraw.box(self.subsurface, (self.xmargin, height-self.ymargin, width-self.xmargin*2, self.ymargin), margin_color)

        if not layout.is_playing:
            draw_margin(BACKGROUND)

        pos = layout.playing_index if layout.is_playing else movie.pos
        highlight = not layout.is_playing and not movie.curr_layer().locked
        surfaces = []

        roi, sub_roi = self.frame_and_subsurface_roi() 
        starting_point = (sub_roi[0], sub_roi[1])
        iwidth, iheight = sub_roi[2], sub_roi[3]
        with draw_bottom_timer:
            surfaces.append(movie.curr_bottom_layers_surface(pos, highlight=highlight, width=iwidth, height=iheight, roi=roi))
        if movie.layers[movie.layer_pos].visible:
            with draw_curr_timer:
                scaled_layer = movie.get_thumbnail(pos, iwidth, iheight, transparent_single_layer=movie.layer_pos, roi=roi)
            surfaces.append(scaled_layer)
        with draw_top_timer:
            surfaces.append(movie.curr_top_layers_surface(pos, highlight=highlight, width=iwidth, height=iheight, roi=roi))

        if not layout.is_playing:
            with draw_light_timer:
                mask = layout.timeline_area().combined_light_table_mask()
            if mask:
                surfaces.append(mask)
            if self.fading_mask:
                with draw_fading_timer:
                    surfaces.append(self.scaled_fading_mask())

        with draw_blits_timer:
            self.subsurface.blits([(surface, starting_point) for surface in surfaces])

        if self.zoom > 1:
            self.draw_zoom_surface()
        else:
            margin_color = UNDRAWABLE if layout.is_playing else MARGIN
            draw_margin(margin_color)

        if layout.is_playing:
            self.restore_zoom_pan_params(zoom_params)

        drawing_area_draw_timer.stop()

    def draw_zoom_surface(self):
        start_x = int(self.zoom_center[0] - self.zoom_surface.get_width()/2)
        start_y = int(self.zoom_center[1] - self.zoom_surface.get_height()/2)
        self.subsurface.blit(self.zoom_surface, (start_x, start_y))
        end_x = start_x + self.zoom_surface.get_width()
        end_y = start_y + self.zoom_surface.get_height()
        pygame.gfxdraw.box(self.subsurface, (0, 0, self.subsurface.get_width(), start_y), MARGIN)
        pygame.gfxdraw.box(self.subsurface, (0, end_y, self.subsurface.get_width(), self.subsurface.get_height()), MARGIN)
        pygame.gfxdraw.box(self.subsurface, (0, start_y, start_x, end_y-start_y), MARGIN)
        pygame.gfxdraw.box(self.subsurface, (end_x, start_y, self.subsurface.get_width(), end_y-start_y), MARGIN)

    def clear_fading_mask(self):
        global last_skeleton
        last_skeleton = None
        self.fading_mask = None
        self.fading_func = None

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
            self.clear_fading_mask()
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
        alt = pg.key.get_mods() & pg.KMOD_ALT
        if alt:
            set_tool(TOOLS['zoom'])
            layout.restore_tool_on_mouse_up = True
        layout.tool.on_mouse_down(*self.fix_xy(x,y))
    def on_mouse_up(self,x,y):
        layout.tool.on_mouse_up(*self.fix_xy(x,y))
    def on_mouse_move(self,x,y):
        layout.tool.on_mouse_move(*self.fix_xy(x,y))

class ScrollIndicator:
    def __init__(self, w, h, vertical=False):
        self.vertical = vertical
        self.surface = pg.Surface((w, h), pg.SRCALPHA)
        scroll_size = (w*2, int(w*2)) if vertical else (int(h*2), h*2)
        self.scroll_left = pg.Surface(scroll_size, pg.SRCALPHA)
        self.scroll_right = pg.Surface(scroll_size, pg.SRCALPHA)

        rgb_left = pg.surfarray.pixels3d(self.scroll_left)
        rgb_right = pg.surfarray.pixels3d(self.scroll_right)

        y, x = np.meshgrid(np.arange(scroll_size[1]), np.arange(scroll_size[0]))
        s = h
        if vertical:
            x, y = y, x
            s = w
        yhdist = np.abs(y-s)/s
        alpha_left = pg.surfarray.pixels_alpha(self.scroll_left)
        alpha_right = pg.surfarray.pixels_alpha(self.scroll_right)
        dist = np.sqrt((y-s)**2 + (x+s/9)**2) # defines the center offset
        dist = np.abs(s/0.78-dist) # defines the ring radius
        dist = np.minimum(dist, s/4.5) # defines the ring width
        mdist = np.max(dist)
        dist /= mdist
        alpha_left[:] = 192*(1-dist)

        dist = np.minimum(dist, 0.5) * 2
        for i in range(3):
            rgb_left[:,:,i] = SELECTED[i]*dist + BACKGROUND[i]*(1-dist)

        if not vertical:
            rgb_right[:] = rgb_left[::-1,:,:]
            alpha_right[:] = alpha_left[::-1,:]
        else:
            rgb_right[:] = rgb_left[:,::-1,:]
            alpha_right[:] = alpha_left[:,::-1]

        self.prev_draw_rect = None
        self.last_dir_change_x = None
        self.last_dir_is_left = None

    def draw(self, surface, px, x, y): # or py, y, x for vertical scroll indicators
        if self.prev_draw_rect is not None:
            try:
                surface.blit(self.surface.subsurface(self.prev_draw_rect), (self.prev_draw_rect[0], self.prev_draw_rect[1]))
            except:
                surface.blit(self.surface, (0, 0))
        y = min(max(y, 0), (surface.get_height() if not self.vertical else surface.get_width())-1)
        if self.last_dir_change_x is None:
            left = px < x
            self.last_dir_change_x = px
        else:
            if abs(x - self.last_dir_change_x) < 10:
                left = self.last_dir_is_left
            else:
                left = self.last_dir_change_x < x
                self.last_dir_change_x = x
        scroll = self.scroll_left if left else self.scroll_right
        if self.vertical:
            starty = x - scroll.get_height()//2
            surface.blit(scroll.subsurface(surface.get_width()-y, 0, scroll.get_width()//2, scroll.get_height()), (0, starty))
            self.prev_draw_rect = (0, starty, scroll.get_width()//2, scroll.get_height())
        else:
            startx = x - scroll.get_width()//2
            surface.blit(scroll.subsurface(0, surface.get_height()-y, scroll.get_width(), scroll.get_height()//2), (startx, 0))
            self.prev_draw_rect = (startx, 0, scroll.get_width(), scroll.get_height()//2)
        self.last_dir_is_left = left

class TimelineArea(LayoutElemBase):
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
        self.eye_open = scale_image(load_image('light_on.png'), eye_icon_size)
        self.eye_shut = scale_image(load_image('light_off.png'), eye_icon_size)

        self.loop_icon = scale_image(load_image('loop.png'), int(screen.get_width()*0.15*0.14))
        self.arrow_icon = scale_image(load_image('arrow.png'), int(screen.get_width()*0.15*0.2))

        self.no_hold = scale_image(load_image('no_hold.png'), int(screen.get_width()*0.15*0.25))
        self.hold_active = scale_image(load_image('hold_yellow.png'), int(screen.get_width()*0.15*0.25))
        self.hold_inactive = scale_image(load_image('hold_grey.png'), int(screen.get_width()*0.15*0.25))

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
        
        self.scroll_indicator = ScrollIndicator(self.subsurface.get_width(), self.subsurface.get_height())

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
            color = (brightness,0,0) if pos_dist < 0 else (0,int(brightness*0.5),0)
            transparency = 0.3
            yield (pos, color, transparency)

    def combined_light_table_mask(self):
        # there are 2 kinds of frame positions: those where the frame of the current layer (at movie.layer_pos) is the same
        # as the frame in the current position (at movie.pos) in that layer due to holds, and those where it's not.
        # for the latter kind, we can combine all their masks produced by movie.get_mask together.
        # for the former kind, we don't get_mask to recompute each position's mask when the current layer changes.
        # so instead we do this:
        #   - we combine all the masks containing all the layers *except* the current one
        #   - we additionally combine all the masks containing all the layers *above* the current one
        #   - we then use the first combined mask at the pixels not covered by the current layer's lines/color alpha at movie.pos,
        #     and we use the second combined mask at the pxiels covered by the current layer's lines/color alpha at movie.pos.
        light_table_positions = list(self.light_table_positions())
        curr_frame = movie.curr_frame()
        curr_layer = movie.layers[movie.layer_pos]
        curr_lit = curr_layer.lit and curr_layer.visible
        held_positions = [(pos,c,t) for pos,c,t in light_table_positions if curr_lit and movie.frame(pos) is curr_frame]
        rest_positions = [(pos,c,t) for pos,c,t in light_table_positions if not (curr_lit and movie.frame(pos) is curr_frame)]

        def combine_masks(masks):
                if len(masks) == 0:
                    return None
                elif len(masks) == 1:
                    return masks[0]
                else:
                    mask = masks[0].copy()
                    alphas = []
                    for m in masks[1:]:
                        alphas.append(m.get_alpha())
                        m.set_alpha(255) # TODO: this assumes the same transparency in all masks - might want to change
                    mask.blits([(m, (0, 0)) for m in masks[1:]])
                    for m,a in zip(masks[1:],alphas):
                        m.set_alpha(a)
                    return mask

        class CachedCombinedMask:
            def __init__(s, light_table_positions, skip_layer=None, lowest_layer_pos=None):
                s.light_table_positions = light_table_positions
                s.skip_layer = skip_layer
                s.lowest_layer_pos = lowest_layer_pos

            def compute_key(s):
                id2version = []
                computation = []
                for pos, color, transparency in s.light_table_positions:
                    i2v, c = movie.get_mask(pos, color, transparency, key=True, lowest_layer_pos=s.lowest_layer_pos, skip_layer=s.skip_layer)
                    id2version += i2v
                    computation.append(c)
                return tuple(id2version), ('combined-mask', tuple(computation))
                
            def compute_value(s):
                masks = []
                for pos, color, transparency in s.light_table_positions:
                    masks.append(movie.get_mask(pos, color, transparency, lowest_layer_pos=s.lowest_layer_pos, skip_layer=s.skip_layer))
                return combine_masks(masks)

        rest_mask = CachedCombinedMask(rest_positions)
        held_mask_outside = CachedCombinedMask(held_positions, skip_layer=movie.layer_pos)
        held_mask_inside = CachedCombinedMask(held_positions, lowest_layer_pos=movie.layer_pos+1)
        da = layout.drawing_area()

        def scaled_key(cached_mask): return da.scale_and_cache(None, cached_mask.compute_key(), get_key=True)
        def scaled(cached_mask):
            k, v = cache.fetch_kv(cached_mask)
            return da.scale_and_cache(v, k)

        class AllPosMask:
            def compute_key(_):
                keys = [scaled_key(m) for m in (rest_mask, held_mask_outside, held_mask_inside)]
                id2vs, comps = zip(*keys)
                id2vs = sum(id2vs,(curr_frame.cache_id_version(),) if held_positions else tuple())
                return id2vs, ('all-pos-mask', tuple(comps))

            def compute_value(_):
                if not held_positions:
                    return scaled(rest_mask)

                held_outside = scaled(held_mask_outside)
                held_inside = scaled(held_mask_inside)
                
                if held_inside or held_outside:
                    roi, sub_roi = da.frame_and_subsurface_roi() 
                    iwidth, iheight = sub_roi[2], sub_roi[3]

                    scaled_layer = movie.get_thumbnail(movie.pos, iwidth, iheight, transparent_single_layer=movie.layer_pos, roi=roi)
                    alpha = pg.surfarray.pixels_alpha(scaled_layer)

                masks = []

                if held_inside:
                    inside = held_inside.copy()
                    ialpha = pg.surfarray.pixels_alpha(inside)
                    ialpha[:] = np.minimum(alpha, ialpha)
                    del ialpha
                    masks.append(inside)

                if held_outside:
                    outside = held_outside.copy()
                    oalpha = pg.surfarray.pixels_alpha(outside)
                    oalpha[:] = np.minimum(255-alpha, oalpha)
                    del oalpha
                    masks.append(outside)
                
                rest = scaled(rest_mask)
                if rest:
                    masks.append(rest)

                return combine_masks(masks)
                
        return cache.fetch(AllPosMask())

    def x2frame(self, x):
        for left, right, pos in self.frame_boundaries:
            if x >= left and x <= right:
                return pos
    def draw(self):
        timeline_area_draw_timer.start()

        surface = self.scroll_indicator.surface
        surface.fill(UNDRAWABLE)

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
            surface.blit(scaled, (x, bottom), (0, 0, thumb_width, height))
            border = 1 + 2*(pos==movie.pos)
            pygame.draw.rect(surface, PEN, (x, bottom, thumb_width, height), border)
            self.frame_boundaries.append((x, x+thumb_width, pos))
            if pos != movie.pos:
                eye = self.eye_open if self.on_light_table.get(pos_dist, False) else self.eye_shut
                eye_x = x + 2 if pos_dist < 0 else x+thumb_width-eye.get_width() - 2
                surface.blit(eye, (eye_x, bottom), eye.get_rect())
                self.eye_boundaries.append((eye_x, bottom, eye_x+eye.get_width(), bottom+eye.get_height(), pos_dist))
            elif len(movie.frames)>1:
                mode = self.loop_icon if self.loop_mode else self.arrow_icon
                mode_x = x + thumb_width - mode.get_width() - 2
                surface.blit(mode, (mode_x, bottom), mode.get_rect())
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

        self.subsurface.blit(surface, (0,0))

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
            hold_bottom = bottom if pos == movie.pos else bottom+height-hold.get_height()
            self.scroll_indicator.surface.blit(hold, (hold_left, hold_bottom), hold.get_rect())
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
        if layout.new_delete_tool():
            if self.x2frame(x) == movie.pos:
                layout.tool.frame_func()
            return
        if self.update_on_light_table(x,y):
            return
        if self.update_loop_mode(x,y):
            return
        if self.update_hold(x,y):
            return
        try_set_cursor(finger_cursor[0])
        self.prevx = x
    def on_mouse_up(self,x,y):
        self.on_mouse_move(x,y)
        if self.prevx:
            restore_cursor()
    def on_mouse_move(self,x,y):
        timeline_move_timer.start()
        self._on_mouse_move(x,y)
        timeline_move_timer.stop()
    def _on_mouse_move(self,x,y):
        self.redraw = False
        x = self.fix_x(x)
        if self.prevx is None:
            return
        if layout.new_delete_tool():
            return
        prev_pos = self.x2frame(self.prevx)
        curr_pos = self.x2frame(x)
        self.scroll_indicator.draw(self.subsurface, self.prevx, x, y)
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
            if self.loop_mode:
                new_pos = (movie.pos + pos_dist) % len(movie.frames)
            else:
                new_pos = min(max(0, movie.pos + pos_dist), len(movie.frames)-1)
            movie.seek_frame(new_pos)

class LayersArea(LayoutElemBase):
    def init(self):
        left, bottom, width, height = self.rect
        max_height = height / MAX_LAYERS
        max_width = IWIDTH * (max_height / IHEIGHT)
        self.width = min(max_width, width)
        self.thumbnail_height = int(self.width * IHEIGHT / IWIDTH)

        self.prevy = None
        self.color_images = {}
        icon_height = min(int(screen.get_width() * 0.15*0.14), self.thumbnail_height / 2)
        self.eye_open = scale_image(load_image('eye_open.png'), height=icon_height)
        self.eye_shut = scale_image(load_image('eye_shut.png'), height=icon_height)
        self.light_on = scale_image(load_image('light_on.png'), height=icon_height)
        self.light_off = scale_image(load_image('light_off.png'), height=icon_height)
        self.locked = scale_image(load_image('locked.png'), height=icon_height)
        self.unlocked = scale_image(load_image('unlocked.png'), height=icon_height)
        self.eye_boundaries = []
        self.lit_boundaries = []
        self.lock_boundaries = []

        self.scroll_indicator = ScrollIndicator(self.subsurface.get_width(), self.subsurface.get_height(), vertical=True)
    
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

        surface = self.scroll_indicator.surface
        surface.fill(UNDRAWABLE)

        self.eye_boundaries = []
        self.lit_boundaries = []
        self.lock_boundaries = []

        left, bottom, width, height = self.rect
        blit_bottom = 0

        for layer_pos, layer in reversed(list(enumerate(movie.layers))):
            border = 1 + (layer_pos == movie.layer_pos)*2
            image = self.cached_image(layer_pos, layer)
            image_left = (width - image.get_width())/2
            pygame.draw.rect(surface, BACKGROUND, (image_left, blit_bottom, image.get_width(), image.get_height()))
            surface.blit(image, (image_left, blit_bottom), image.get_rect()) 
            pygame.draw.rect(surface, PEN, (image_left, blit_bottom, image.get_width(), image.get_height()), border)

            max_border = 3
            if len(movie.frames) > 1 and layer.visible and list(layout.timeline_area().light_table_positions()):
                lit = self.light_on if layer.lit else self.light_off
                surface.blit(lit, (width - lit.get_width() - max_border, blit_bottom))
                self.lit_boundaries.append((left + width - lit.get_width() - max_border, bottom, left+width, bottom+lit.get_height(), layer_pos))
               
            eye = self.eye_open if layer.visible else self.eye_shut
            surface.blit(eye, (width - eye.get_width() - max_border, blit_bottom + image.get_height() - eye.get_height() - max_border))
            self.eye_boundaries.append((left + width - eye.get_width() - max_border, bottom + image.get_height() - eye.get_height() - max_border, left+width, bottom+image.get_height(), layer_pos))

            lock = self.locked if layer.locked else self.unlocked
            lock_start = self.thumbnail_height/2 - lock.get_height()/2
            surface.blit(lock, (0, blit_bottom + lock_start))
            self.lock_boundaries.append((left, bottom + lock_start, left+lock.get_width(), bottom + lock_start+lock.get_height(), layer_pos))

            bottom += image.get_height()
            blit_bottom += image.get_height()

        self.subsurface.blit(surface, (0, 0))

        layers_area_draw_timer.stop()

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
        if layout.new_delete_tool():
            if self.y2frame(y) == movie.layer_pos:
                layout.tool.layer_func()
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
            try_set_cursor(finger_cursor[0])
    def on_mouse_up(self,x,y):
        self.on_mouse_move(x,y)
        if self.prevy:
            restore_cursor()
    def on_mouse_move(self,x,y):
        self.redraw = False
        if self.prevy is None:
            return
        if layout.new_delete_tool():
            return
        self.scroll_indicator.draw(self.subsurface, self.prevy-self.rect[1], y-self.rect[1], x-self.rect[0])
        prev_pos = self.y2frame(self.prevy)
        curr_pos = self.y2frame(y)
        if curr_pos is None or curr_pos < 0 or curr_pos >= len(movie.layers):
            return
        self.prevy = y
        pos_dist = curr_pos - prev_pos
        if pos_dist != 0:
            self.redraw = True
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
        done_width = min(full_width, int(full_width * (self.done/max(1,self.total))))
        pg.draw.rect(screen, PROGRESS, (left, bottom, done_width, height))
        text_surface = font.render(self.title, True, UNUSED)
        pos = ((full_width-text_surface.get_width())/2+left, (height-text_surface.get_height())/2+bottom)
        screen.blit(text_surface, pos)
        pg.display.flip()
        pg.event.pump()

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
            image = load_image(frame_file) if os.path.exists(frame_file) else new_frame()
            # FIXME: take the aspect ratio from the json file into account
            # TODO: avoid reloading images if the file didn't change since the last time
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
        layout.drawing_area().reset_zoom_pan_params()
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

class MovieListArea(LayoutElemBase):
    def init(self):
        self.show_pos = None
        self.prevx = None
        self.scroll_indicator = ScrollIndicator(self.subsurface.get_width(), self.subsurface.get_height())
    def draw(self):
        movie_list_area_draw_timer.start()

        surface = self.scroll_indicator.surface
        surface.fill(UNDRAWABLE)

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
            surface.blit(image, (left, 0), image.get_rect()) 
            pygame.draw.rect(surface, PEN, (left, 0, image.get_width(), image.get_height()), border)
            left += image.get_width()
            if left >= width:
                break

        self.subsurface.blit(surface, (0, 0))

        movie_list_area_draw_timer.stop()
    def x2frame(self, x):
        if not movie_list.images or x is None:
            return None
        left, _, _, _ = self.rect
        return (x-left) // movie_list.images[0].get_width()
    def on_mouse_down(self,x,y):
        self.prevx = None
        if layout.new_delete_tool():
            if self.x2frame(x) == 0:
                layout.tool.clip_func()
            return
        self.prevx = x
        self.show_pos = movie_list.clip_pos
        try_set_cursor(finger_cursor[0])
    def on_mouse_move(self,x,y):
        self.redraw = False
        if self.prevx is None:
            self.prevx = x # this happens eg when a new_delete_tool is used upon mouse down
            # and then the original tool is restored
            self.show_pos = movie_list.clip_pos
        if layout.new_delete_tool():
            return
        self.scroll_indicator.draw(self.subsurface, self.prevx-self.rect[0], x-self.rect[0], y-self.rect[1])
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
        self.subsurface.blit(self.scroll_indicator.surface, (0, 0))
        # opening a movie is a slow operation so we don't want it to be "too interactive"
        # (like timeline scrolling) - we wait for the mouse-up event to actually open the clip
        movie_list.open_clip(self.show_pos)
        if self.prevx is not None:
            restore_cursor()
        self.prevx = None
        self.show_pos = None

class ToolSelectionButton(LayoutElemBase):
    def __init__(self, tool):
        LayoutElemBase.__init__(self)
        self.tool = tool
    def draw(self):
        pg.draw.rect(screen, SELECTED if self.tool is layout.full_tool else UNDRAWABLE, self.rect)
        self.tool.tool.draw(self.rect,self.tool.cursor[1])
    def hit(self,x,y): return self.tool.tool.hit(x,y,self.rect)
    def on_mouse_down(self,x,y):
        set_tool(self.tool)
    def on_mouse_up(self,x,y): pass
    def on_mouse_move(self,x,y): pass

class TogglePlaybackButton(Button):
    def __init__(self, play_icon, pause_icon):
        self.play = play_icon
        self.pause = pause_icon
        Button.__init__(self)
        # this is the one button which has a simple, big and round icon, and you don't to be too easy
        # to hit. the others, when we make it necessary to hit the non-transparent part, get annoying -
        # the default behavior is better since imprecise hits select _something_ and then you learn to
        # improve your aim by figuring out what was hit, whereas when nothing is selected you have
        # a harder time learning, apparently
        self.only_hit_non_transparent = True
    def draw(self):
        icon = self.pause if layout.is_playing else self.play
        self.button_surface = None
        Button.draw(self, self.rect, icon)
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

    def get_mask(self, pos, rgb, transparency, key=False, lowest_layer_pos=None, skip_layer=None):
        # ignore invisible layers
        if lowest_layer_pos is None:
            lowest_layer_pos = 0
        layers = [layer for i,layer in enumerate(self.layers) if layer.visible and i>=lowest_layer_pos and i!=skip_layer]
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
                mask_surface.fill(rgb)
                pg.surfarray.pixels_alpha(mask_surface)[:] = cache.fetch(CachedMaskAlpha())
                mask_surface.set_alpha(int(transparency*255))
                return mask_surface

        if key:
            return CachedMask().compute_key()
        return cache.fetch(CachedMask())

    def _visible_layers_id2version(self, layers, pos, include_invisible=False):
        frames = [layer.frame(pos) for layer in layers if layer.visible or include_invisible]
        return tuple([frame.cache_id_version() for frame in frames if not frame.empty()])

    def get_thumbnail(self, pos, width, height, highlight=True, transparent_single_layer=-1, roi=None):
        if roi is None:
            roi = (0, 0, IWIDTH, IHEIGHT) # the roi is in the original image coordinates, not the thumbnail coordinates
        trans_single = transparent_single_layer >= 0
        layer_pos = self.layer_pos if not trans_single else transparent_single_layer
        def id2version(layers): return self._visible_layers_id2version(layers, pos, include_invisible=trans_single)

        class CachedThumbnail(CachedItem):
            def compute_key(_):
                if trans_single:
                    return id2version([self.layers[layer_pos]]), ('transparent-layer-thumbnail', width, height, roi)
                else:
                    def layer_ids(layers): return tuple([layer.id for layer in layers if not layer.frame(pos).empty()])
                    hl = ('highlight', layer_ids(self.layers[:layer_pos]), layer_ids([self.layers[layer_pos]]), layer_ids(self.layers[layer_pos+1:])) if highlight else 'no-highlight'
                    return id2version(self.layers), ('thumbnail', width, height, roi, hl)
            def compute_value(_):
                h = int(screen.get_height() * 0.15)
                w = int(h * IWIDTH / IHEIGHT)
                if w <= width and h <= height:
                    if trans_single:
                        return self.layers[layer_pos].frame(pos).thumbnail(width, height, roi)

                    s = self.curr_bottom_layers_surface(pos, highlight=highlight, width=width, height=height, roi=roi).copy()
                    if self.layers[self.layer_pos].visible:
                        s.blit(self.get_thumbnail(pos, width, height, transparent_single_layer=layer_pos, roi=roi), (0, 0))
                    s.blit(self.curr_top_layers_surface(pos, highlight=highlight, width=width, height=height, roi=roi), (0, 0))
                    return s
                else:
                    return scale_image(self.get_thumbnail(pos, w, h, highlight=highlight, transparent_single_layer=transparent_single_layer, roi=roi), width, height)

        return cache.fetch(CachedThumbnail())

    def clear_cache(self):
        layout.drawing_area().clear_fading_mask()

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

    def curr_bottom_layers_surface(self, pos, highlight, width=None, height=None, roi=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT
        if not roi: roi=(0, 0, IWIDTH, IHEIGHT)

        class CachedBottomLayers:
            def compute_key(_):
                return self._visible_layers_id2version(self.layers[:self.layer_pos], pos), ('blit-bottom-layers' if not highlight else 'bottom-layers-highlighted', width, height, roi)
            def compute_value(_):
                layers = self._blit_layers(self.layers[:self.layer_pos], pos, transparent=True, width=width, height=height, roi=roi)
                s = pg.Surface((width, height), pg.SRCALPHA)
                s.fill(BACKGROUND)
                if self.layer_pos == 0:
                    return s
                if not highlight:
                    s.blit(layers, (0, 0))
                    return s

                layers.set_alpha(128)
                da = layout.drawing_area()
                w, h = da.iwidth+da.xmargin*2, da.iheight+da.ymargin*2
                class BelowImage:
                    def compute_key(_): return tuple(), ('below-image', w, h)
                    def compute_value(_):
                        below_image = pg.Surface((w, h), pg.SRCALPHA)
                        below_image.set_alpha(128)
                        below_image.fill(LAYERS_BELOW)
                        return below_image
                rgba = np.copy(rgba_array(layers)[0]) # funnily enough, this is much faster than calling array_alpha()
                # to save a copy of just the alpha pixels [those we really need]...
                layers.blit(cache.fetch(BelowImage()), (0,0))
                rgba_array(layers)[0][:,:,3] = rgba[:,:,3]
                self._set_undrawable_layers_grid(layers)
                s.blit(layers, (0,0))

                return s

        return cache.fetch(CachedBottomLayers())

    def curr_top_layers_surface(self, pos, highlight, width=None, height=None, roi=None):
        if not width: width=IWIDTH
        if not height: height=IHEIGHT
        if not roi: roi=(0, 0, IWIDTH, IHEIGHT)

        class CachedTopLayers:
            def compute_key(_):
                return self._visible_layers_id2version(self.layers[self.layer_pos+1:], pos), ('blit-top-layers' if not highlight else 'top-layers-highlighted', width, height, roi)
            def compute_value(_):
                layers = self._blit_layers(self.layers[self.layer_pos+1:], pos, transparent=True, width=width, height=height, roi=roi)
                if not highlight or self.layer_pos == len(self.layers)-1:
                    return layers

                layers.set_alpha(128)
                s = pg.Surface((width, height), pg.SRCALPHA)
                s.fill(BACKGROUND)
                da = layout.drawing_area()
                w, h = da.iwidth+da.xmargin*2, da.iheight+da.ymargin*2
                class AboveImage:
                    def compute_key(_): return tuple(), ('above-image', w, h)
                    def compute_value(_):
                        above_image = pg.Surface((w, h), pg.SRCALPHA)
                        above_image.set_alpha(128)
                        above_image.fill(LAYERS_ABOVE)
                        return above_image
                rgba = np.copy(rgba_array(layers)[0])
                layers.blit(cache.fetch(AboveImage()), (0,0))
                self._set_undrawable_layers_grid(layers)
                s.blit(layers, (0,0))
                rgba_array(s)[0][:,:,3] = rgba[:,:,3]
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

        if self.edited_since_export or not self.exported_files_exist():

            # remove old pngs so we don't have stale ones lying around that don't correspond to a valid frame;
            # also, we use them for getting the status of the export progress...
            for f in os.listdir(self.dir):
                if is_exported_png(f):
                    os.unlink(os.path.join(self.dir, f))

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

class InsertFrameHistoryItem(HistoryItemBase):
    def __init__(self):
        HistoryItemBase.__init__(self)
    def undo(self):
        # normally remove_frame brings you to the next frame after the one you removed.
        # but when undoing insert_frame, we bring you to the previous frame after the one
        # you removed - it's the one where you inserted the frame we're now removing to undo
        # the insert, so this is where we should go to bring you back in time.
        removed_frame_data = movie.remove_frame(at_pos=self.pos_before_undo, new_pos=max(0, self.pos_before_undo-1))
        return RemoveFrameHistoryItem(self.pos_before_undo, removed_frame_data)
    def __str__(self):
        return f'InsertFrameHistoryItem(removing at pos {self.pos_before_undo})'

class RemoveFrameHistoryItem(HistoryItemBase):
    def __init__(self, pos, removed_frame_data):
        HistoryItemBase.__init__(self, restore_pos_before_undo=False)
        self.pos = pos
        self.removed_frame_data = removed_frame_data
    def undo(self):
        movie.reinsert_frame_at_pos(self.pos, self.removed_frame_data)
        return InsertFrameHistoryItem()
    def __str__(self):
        return f'RemoveFrameHistoryItem(inserting at pos {self.pos})'
    def byte_size(self):
        frames, holds = self.removed_frame_data
        return sum([f.size() for f in frames])

class InsertLayerHistoryItem(HistoryItemBase):
    def __init__(self):
        HistoryItemBase.__init__(self)
    def undo(self):
        removed_layer = movie.remove_layer(at_pos=self.layer_pos_before_undo, new_pos=max(0, self.layer_pos_before_undo-1))
        return RemoveLayerHistoryItem(self.layer_pos_before_undo, removed_layer)
    def __str__(self):
        return f'InsertLayerHistoryItem(removing layer {self.layer_pos_before_undo})'

class RemoveLayerHistoryItem(HistoryItemBase):
    def __init__(self, layer_pos, removed_layer):
        HistoryItemBase.__init__(self, restore_pos_before_undo=False)
        self.layer_pos = layer_pos
        self.removed_layer = removed_layer
    def undo(self):
        movie.reinsert_layer_at_pos(self.layer_pos, self.removed_layer)
        return InsertLayerHistoryItem()
    def __str__(self):
        return f'RemoveLayerHistoryItem(inserting layer {self.layer_pos})'
    def byte_size(self):
        return sum([f.size() for f in self.removed_layer.frames])

class ToggleHoldHistoryItem(HistoryItemBase):
    def __init__(self): HistoryItemBase.__init__(self)
    def undo(self):
        movie.toggle_hold()
        return self
    def __str__(self):
        return f'ToggleHoldHistoryItem(toggling hold at frame {self.pos_before_undo} layer {self.layer_pos_before_undo})'

class ToggleHistoryItem(HistoryItemBase):
    def __init__(self, toggle_func):
        HistoryItemBase.__init__(self) # ATM the toggles we use require to seek
        # to the original movie position before undoing - could make this more parameteric if needed
        self.toggle_func = toggle_func
    def undo(self):
        self.toggle_func()
        return self
    def __str__(self):
        return f'ToggleHistoryItem({self.toggle_func.__qualname__})'

def insert_frame():
    movie.insert_frame()
    history.append_item(InsertFrameHistoryItem())

def insert_layer():
    movie.insert_layer()
    history.append_item(InsertLayerHistoryItem())

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
    movie.next_frame()

def prev_frame():
    if movie.pos <= 0 and not layout.timeline_area().loop_mode:
        return
    movie.prev_frame()

def insert_clip():
    global movie
    movie.save_before_closing()
    movie = Movie(new_movie_clip_dir())
    movie.render_and_save_current_frame() # write out CURRENT_FRAME_FILE for MovieListArea.reload...
    movie_list.reload()

def clips_were_inserted_via_filesystem():
    global movie
    movie.save_before_closing()
    movie_list.reload()
    movie = Movie(default_clip_dir()[0])

def remove_clip():
    if len(movie_list.clips) <= 1:
        return # we don't remove the last clip - if we did we'd need to create a blank one,
        # which is a bit confusing. [we can't remove the last frame in a timeline, either]
    global movie
    movie.save_before_closing()
    movie_list.interrupt_export()
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
        history.append_item(ToggleHoldHistoryItem())

def toggle_layer_lock():
    layer = movie.curr_layer()
    layer.toggle_locked()
    history.append_item(ToggleHistoryItem(layer.toggle_locked))

def zoom_to_film_res():
    x, y = pg.mouse.get_pos()
    da = layout.drawing_area()
    left, bottom, width, height = da.rect
    da.set_zoom_to_film_res((x-left,y-bottom))

TOOLS = {
    'zoom': Tool(ZoomTool(), zoom_cursor, 'zZ'),
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
    'insert-frame': (insert_frame, '=+', load_image('sheets.png')),
    'remove-frame': (remove_frame, '-_', load_image('garbage.png')),
    'next-frame': (next_frame, '.<', None),
    'prev-frame': (prev_frame, ',>', None),
    'toggle-playing': (toggle_playing, '\r', None),
    'toggle-loop-mode': (toggle_loop_mode, 'c', None),
    'toggle-frame-hold': (toggle_frame_hold, 'h', None),
    'toggle-layer-lock': (toggle_layer_lock, 'l', None),
    'zoom-to-film-res': (zoom_to_film_res, '1', None),
}

tool_change = 0
prev_tool = None
def set_tool(tool):
    global prev_tool
    global tool_change
    prev = layout.full_tool
    layout.tool = tool.tool
    layout.full_tool = tool
    if not isinstance(prev.tool, NewDeleteTool):
        prev_tool = prev
    if tool.cursor:
        try_set_cursor(tool.cursor[0])
    tool_change += 1

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
    def __init__(self, filename, rows=11, columns=3):
        s = load_image(filename)
        color_hist = {}
        first_color_hit = {}
        for y in range(s.get_height()):
            for x in range(s.get_width()):
                r,g,b,a = s.get_at((x,y))
                color = r,g,b
                if color not in first_color_hit:
                    first_color_hit[color] = (y / (s.get_height()/3))*s.get_width() + x
                color_hist[color] = color_hist.get(color,0) + 1

        colors = [[None for col in range(columns)] for row in range(rows)]
        color2popularity = dict(list(reversed(sorted(list(color_hist.items()), key=lambda x: x[1])))[:rows*columns])
        hit2color = [(first_hit, color) for color, first_hit in sorted(list(first_color_hit.items()), key=lambda x: x[1])]

        row = 0
        col = 0
        for hit, color in hit2color:
            if color in color2popularity:
                colors[row][col] = color + (255,)
                row+=1
                if row == rows:
                    col += 1
                    row = 0

        self.bg_color = BACKGROUND+(0,)

        self.rows = rows
        self.columns = columns
        self.colors = colors

        self.init_cursors()

    def init_cursors(self):
        radius = PAINT_BUCKET_WIDTH//2
        def bucket(color): return add_circle(color_image(paint_bucket_cursor[0], color), radius)

        self.cursors = [[None for col in range(self.columns)] for row in range(self.rows)]
        for row in range(self.rows):
            for col in range(self.columns):
                sc = bucket(self.colors[row][col])
                self.cursors[row][col] = (pg.cursors.Cursor((radius,sc.get_height()-radius-1), sc), color_image(paint_bucket_cursor[1], self.colors[row][col]))

        sc = bucket(self.bg_color)
        cursor = (pg.cursors.Cursor((radius,sc.get_height()-radius-1), sc), color_image(paint_bucket_cursor[1], self.bg_color))
        self.bg_cursor = (cursor[0], scale_image(load_image('water-tool.png'), cursor[1].get_width()))

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

class EmptyElem(LayoutElemBase):
    def draw(self): pg.draw.rect(screen, UNUSED, self.rect)

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
            tool = None
            if row == len(palette.colors):
                if col == 0:
                    tool = TOOLS['zoom']
                elif col == 1:
                    tool = Tool(PaintBucketTool(palette.bg_color), palette.bg_cursor, '')
                else:
                    continue
            if not tool:
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
            button = TogglePlaybackButton(load_image('play.png'), load_image('pause.png'))
        else:
            button = ToolSelectionButton(TOOLS[func])
        layout.add((TOOLBAR_X_START+offset*0.15,0.15,width*0.15, 0.1), button)
        offset += width

    set_tool(last_tool if last_tool else TOOLS['pencil'])

def new_movie_clip_dir(): return os.path.join(WD, format_now())

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

class SwapWidthHeightHistoryItem(HistoryItemBase):
    def __init__(self): HistoryItemBase.__init__(self, restore_pos_before_undo=False)
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

class History:
    # a history is kept per movie. the size of the history is global - we don't
    # want to exceed a certain memory threshold for the history
    byte_size = 0
    
    def __init__(self):
        self.undo = []
        self.redo = []
        layout.drawing_area().clear_fading_mask()
        self.suggestions = None

    def __del__(self):
        for op in self.undo + self.redo:
            History.byte_size -= op.byte_size()

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
        if item is None or item.nop():
            return

        self._merge_prev_suggestions()

        self.undo.append(item)
        History.byte_size += item.byte_size() - sum([op.byte_size() for op in self.redo])
        self.redo = [] # forget the redo stack
        while self.undo and History.byte_size > MAX_HISTORY_BYTE_SIZE:
            History.byte_size -= self.undo[0].byte_size()
            del self.undo[0]

        layout.drawing_area().clear_fading_mask() # new operations invalidate old skeletons

    def undo_item(self, drawing_changes_only):
        if self.suggestions:
            s = self.suggestions
            self.suggestions = None
            for item in s:
                self.append_item(item)

        if self.undo:
            last_op = self.undo[-1]
            if drawing_changes_only and (not last_op.is_drawing_change() or not last_op.from_curr_pos()):
                return

            if last_op.make_undone_changes_visible():
                return # we had to seek to the location of the changes about to be undone - let the user
                # see the state before the undoing, next time the user asks for undo we'll actually undo
                # and it will be clear what the undoing did

            redo = last_op.undo()
            History.byte_size += redo.byte_size() - last_op.byte_size()
            if redo is not None:
                self.redo.append(redo)
            self.undo.pop()

        layout.drawing_area().clear_fading_mask() # changing canvas state invalidates old skeletons

    def redo_item(self):
        if self.redo:
            last_op = self.redo[-1]
            if last_op.make_undone_changes_visible():
                return
            undo = last_op.undo()
            History.byte_size += undo.byte_size() - last_op.byte_size()
            if undo is not None:
                self.undo.append(undo)
            self.redo.pop()

    def clear(self):
        History.byte_size -= sum([op.byte_size() for op in self.undo+self.redo])
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

user_event_offset = 0
def user_event():
    global user_event_offset
    user_event_offset += 1
    return user_event_offset

PLAYBACK_TIMER_EVENT = user_event()
SAVING_TIMER_EVENT = user_event() 
FADING_TIMER_EVENT = user_event()
PAINTING_TIMER_EVENT = user_event()
HISTORY_TIMER_EVENT = user_event()

pygame.time.set_timer(PLAYBACK_TIMER_EVENT, 1000//FRAME_RATE) # we play back at 12 fps
pygame.time.set_timer(SAVING_TIMER_EVENT, 15*1000) # we save the current frame every 15 seconds
pygame.time.set_timer(FADING_TIMER_EVENT, 1000//FADING_RATE)

timer_events = [
    PLAYBACK_TIMER_EVENT,
    SAVING_TIMER_EVENT,
    FADING_TIMER_EVENT,
    PAINTING_TIMER_EVENT,
    HISTORY_TIMER_EVENT,
]

REDRAW_LAYOUT_EVENT = user_event() 
RELOAD_MOVIE_LIST_EVENT = user_event()

interesting_event_attrs = 'type pos key mod rep'.split() # rep means "replayed event" - we log these same as others
interesting_events = [
    pygame.KEYDOWN,
    pygame.KEYUP,
    pygame.MOUSEMOTION,
    pygame.MOUSEBUTTONDOWN,
    pygame.MOUSEBUTTONUP,
    REDRAW_LAYOUT_EVENT,
    RELOAD_MOVIE_LIST_EVENT,
] + timer_events

event2timer = {}
event_names = 'KEYDOWN KEYUP MOVE DOWN UP REDRAW RELOAD PLAYBACK SAVING FADING PAINTING HISTORY'.split()
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

def open_explorer(path):
    if on_windows:
        subprocess.Popen(['explorer', '/select,'+path])
    else:
        subprocess.Popen(['nautilus', '-s', path])

def export_and_open_explorer():
    movie.save_and_start_export()
    movie_list.wait_for_all_exporting_to_finish() # wait for this movie and others if we
    # were still exporting them - so that when we open explorer all the exported data is up to date
    movie.edited_since_export = False

    open_explorer(movie.gif_path())

def open_dir_path_dialog():
    dialog_subprocess = subprocess.Popen([sys.executable, sys.argv[0], 'dir-path-dialog'], stdout=subprocess.PIPE)
    output, _ = dialog_subprocess.communicate()
    # we use repr/eval because writing Unicode to sys.stdout fails
    # and so does writing the binary output of encode() without repr()
    file_path = eval(output).decode() if output.strip() else None
    return file_path

def open_clip_dir():
    file_path = open_dir_path_dialog()
    global WD
    if file_path and os.path.realpath(file_path) != os.path.realpath(WD):
        movie.save_before_closing()
        movie_list.wait_for_all_exporting_to_finish()
        set_wd(file_path)
        load_clips_dir()

def process_keyup_event(event):
    ctrl = event.mod & pg.KMOD_CTRL
    if not ctrl and isinstance(layout.tool, FlashlightTool):
        try_set_cursor(flashlight_cursor[0])

def process_keydown_event(event):
    ctrl = event.mod & pg.KMOD_CTRL
    shift = event.mod & pg.KMOD_SHIFT

    if ctrl and isinstance(layout.tool, FlashlightTool):
        try_set_cursor(needle_cursor[0])

    # Like Escape, Undo/Redo and Delete History are always available thru the keyboard [and have no other way to access them]
    if event.key == pg.K_SPACE:
        if ctrl:
            history.redo_item()
            return
        else:
            history.undo_item(drawing_changes_only=True)
            return

    # Ctrl-Z: undo any change (space only undoes drawing changes and does nothing if the latest change in the history
    # isn't a drawing change)
    if event.key == pg.K_z and ctrl:
        history.undo_item(drawing_changes_only=False)
        return

    # Ctrl+Shift+Delete
    if event.key == pg.K_DELETE and ctrl and shift:
        clear_history()
        return

    # Ctrl-E: export
    if ctrl and event.key == pg.K_e:
        export_and_open_explorer()
        return

    # Ctrl-O: open a directory
    if ctrl and event.key == pg.K_o:
        open_clip_dir()
        return

    # Ctrl-C/X/V
    if ctrl:
        if event.key == pg.K_c:
            copy_frame()
            return
        elif event.key == pg.K_x:
            cut_frame()
            return
        elif event.key == pg.K_v:
            paste_frame()
            return

    # Ctrl-R: rotate
    if ctrl and event.key == pg.K_r:
        swap_width_height()
        return

    # teacher/student - TODO: better UI
    # Ctrl-T: teacher client
    if ctrl and event.key == pg.K_t:
        print('shutting down the student server and starting the teacher client')
        start_teacher_client()
        return
    if ctrl and event.key == pg.K_l and teacher_client:
        print('locking student screens')
        teacher_client.lock_screens()
        return
    if ctrl and event.key == pg.K_u and teacher_client:
        print('unlocking student screens')
        teacher_client.unlock_screens()
        return
    if ctrl and event.key == pg.K_b and teacher_client:
        print('saving class backup')
        teacher_client.save_class_backup()
        return
    if ctrl and event.key == pg.K_d and teacher_client:
        print('restoring class backup')
        teacher_client.restore_class_backup(open_dir_path_dialog())
        return
    if ctrl and event.key == pg.K_p and teacher_client:
        print("putting a directory in all students' Tinymation directories")
        teacher_client.put_dir(open_dir_path_dialog())
        return

    # Ctrl-1/2: set layout to drawing/animation
    if ctrl and event.key == pg.K_1:
        layout.mode = DRAWING_LAYOUT
        if teacher_client:
            teacher_client.drawing_layout()
        return
    if ctrl and event.key == pg.K_2:
        layout.mode = ANIMATION_LAYOUT
        if teacher_client:
            teacher_client.animation_layout()
        return

    # other keyboard shortcuts are enabled/disabled by Ctrl-A
    global keyboard_shortcuts_enabled

    if keyboard_shortcuts_enabled:
        for tool in TOOLS.values():
            if event.key in [ord(c) for c in tool.chars]:
                set_tool(tool)
                return

        for func, chars, _ in FUNCTIONS.values():
            if event.key in [ord(c) for c in chars]:
                func()
                return
                
    if event.key == pygame.K_a and ctrl:
        keyboard_shortcuts_enabled = not keyboard_shortcuts_enabled
        print('Ctrl-A pressed -','enabling' if keyboard_shortcuts_enabled else 'disabling','keyboard shortcuts')

layout.draw()
pygame.display.flip()

export_on_exit = True

class ScreenLock:
    def __init__(self):
        self.locked = False

    def is_locked(self):
        if student_server is not None and student_server.lock_screen:
            layout.draw_locked()
            pygame.display.flip()
            try_set_cursor(empty_cursor)
            self.locked = True
            return True
        elif self.locked:
            layout.draw()
            pygame.display.flip()
            try_set_cursor(layout.full_tool.cursor[0])
            self.locked = False
        return False

replayed_event_index = 0

try:
    screen_lock = ScreenLock()
    while not escape: 
        try:
            event = replay_log.events[replayed_event_index]
            replayed_event_index += 1
            events = [event]
            if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                raise Exception('not quitting')
        except:
            # pygame.event.get() returns an empty list when there are no events,
            # so a loop using it uses 100% of CPU
            events = [pygame.event.wait()]
        for event in events:
            if event.type not in interesting_events:
                continue
            
            if event.type in [pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP] and getattr(event, 'button', 1) != 1:
                continue
            if event.type == pg.MOUSEMOTION and getattr(event, 'buttons', (1,0,0)) != (1,0,0):
                continue

            if screen_lock.is_locked():
                continue

            event_attrs = {}
            for attr in interesting_event_attrs:
                val = getattr(event, attr, None)
                if val is not None:
                    event_attrs[attr] = val
            log_event(event_attrs)
            #pickle.dump(event_attrs, event_log)

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

                    if layout.is_playing and not (keyboard_shortcuts_enabled and event.key == pygame.K_RETURN):
                        continue # ignore keystrokes (except ESC and ENTER) during playback

                    timer.start()
                    process_keydown_event(event)

                else:
                    timer.start()

                    if event.type == pygame.KEYUP:
                        process_keyup_event(event)
                    else:
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
if student_server:
    student_server.stop()

print('>>> QUITTING',format_now())
