from math import floor
import os
from os.path import join as pjoin
import warnings
from warnings import warn

import numpy as np

import nibabel as nib

from mayavi import mlab
from mayavi.tools.mlab_scene_model import MlabSceneModel
from mayavi.core import lut_manager
from mayavi.core.scene import Scene
from mayavi.core.ui.api import SceneEditor
from mayavi.core.ui.mayavi_scene import MayaviScene
from traits.api import (HasTraits, Range, Int, Float,
                        Bool, Enum, on_trait_change, Instance)
from tvtk.api import tvtk

from . import utils, io
from .utils import (Surface, verbose, create_color_lut, _get_subjects_dir,
                    string_types, threshold_filter)


import logging
logger = logging.getLogger('surfer')


lh_viewdict = {'lateral': {'v': (180., 90.), 'r': 90.},
               'medial': {'v': (0., 90.), 'r': -90.},
               'rostral': {'v': (90., 90.), 'r': -180.},
               'caudal': {'v': (270., 90.), 'r': 0.},
               'dorsal': {'v': (180., 0.), 'r': 90.},
               'ventral': {'v': (180., 180.), 'r': 90.},
               'frontal': {'v': (120., 80.), 'r': 106.739},
               'parietal': {'v': (-120., 60.), 'r': 49.106}}
rh_viewdict = {'lateral': {'v': (180., -90.), 'r': -90.},
               'medial': {'v': (0., -90.), 'r': 90.},
               'rostral': {'v': (-90., -90.), 'r': 180.},
               'caudal': {'v': (90., -90.), 'r': 0.},
               'dorsal': {'v': (180., 0.), 'r': 90.},
               'ventral': {'v': (180., 180.), 'r': 90.},
               'frontal': {'v': (60., 80.), 'r': -106.739},
               'parietal': {'v': (-60., 60.), 'r': -49.106}}
viewdicts = dict(lh=lh_viewdict, rh=rh_viewdict)


def make_montage(filename, fnames, orientation='h', colorbar=None,
                 border_size=15):
    """Save montage of current figure

    Parameters
    ----------
    filename : str
        The name of the file, e.g, 'montage.png'. If None, the image
        will not be saved.
    fnames : list of str | list of array
        The images to make the montage of. Can be a list of filenames
        or a list of image data arrays.
    orientation : 'h' | 'v' | list
        The orientation of the montage: horizontal, vertical, or a nested
        list of int (indexes into fnames).
    colorbar : None | list of int
        If None remove colorbars, else keep the ones whose index
        is present.
    border_size : int
        The size of the border to keep.

    Returns
    -------
    out : array
        The montage image data array.
    """
    try:
        import Image
    except (ValueError, ImportError):
        from PIL import Image
    from scipy import ndimage
    # This line is only necessary to overcome a PIL bug, see:
    #     http://stackoverflow.com/questions/10854903/what-is-causing-
    #          dimension-dependent-attributeerror-in-pil-fromarray-function
    fnames = [f if isinstance(f, string_types) else f.copy() for f in fnames]
    if isinstance(fnames[0], string_types):
        images = list(map(Image.open, fnames))
    else:
        images = list(map(Image.fromarray, fnames))
    # get bounding box for cropping
    boxes = []
    for ix, im in enumerate(images):
        # sum the RGB dimension so we do not miss G or B-only pieces
        gray = np.sum(np.array(im), axis=-1)
        gray[gray == gray[0, 0]] = 0  # hack for find_objects that wants 0
        if np.all(gray == 0):
            raise ValueError("Empty image (all pixels have the same color).")
        labels, n_labels = ndimage.label(gray.astype(np.float))
        slices = ndimage.find_objects(labels, n_labels)  # slice roi
        if colorbar is not None and ix in colorbar:
            # we need all pieces so let's compose them into single min/max
            slices_a = np.array([[[xy.start, xy.stop] for xy in s]
                                 for s in slices])
            # TODO: ideally gaps could be deduced and cut out with
            #       consideration of border_size
            # so we need mins on 0th and maxs on 1th of 1-nd dimension
            mins = np.min(slices_a[:, :, 0], axis=0)
            maxs = np.max(slices_a[:, :, 1], axis=0)
            s = (slice(mins[0], maxs[0]), slice(mins[1], maxs[1]))
        else:
            # we need just the first piece
            s = slices[0]
        # box = (left, top, width, height)
        boxes.append([s[1].start - border_size, s[0].start - border_size,
                      s[1].stop + border_size, s[0].stop + border_size])
    # convert orientation to nested list of int
    if orientation == 'h':
        orientation = [range(len(images))]
    elif orientation == 'v':
        orientation = [[i] for i in range(len(images))]
    # find bounding box
    n_rows = len(orientation)
    n_cols = max(len(row) for row in orientation)
    if n_rows > 1:
        min_left = min(box[0] for box in boxes)
        max_width = max(box[2] for box in boxes)
        for box in boxes:
            box[0] = min_left
            box[2] = max_width
    if n_cols > 1:
        min_top = min(box[1] for box in boxes)
        max_height = max(box[3] for box in boxes)
        for box in boxes:
            box[1] = min_top
            box[3] = max_height
    # crop images
    cropped_images = []
    for im, box in zip(images, boxes):
        cropped_images.append(im.crop(box))
    images = cropped_images
    # Get full image size
    row_w = [sum(images[i].size[0] for i in row) for row in orientation]
    row_h = [max(images[i].size[1] for i in row) for row in orientation]
    out_w = max(row_w)
    out_h = sum(row_h)
    # compose image
    new = Image.new("RGBA", (out_w, out_h))
    y = 0
    for row, h in zip(orientation, row_h):
        x = 0
        for i in row:
            im = images[i]
            pos = (x, y)
            new.paste(im, pos)
            x += im.size[0]
        y += h
    if filename is not None:
        new.save(filename)
    return np.array(new)


def _prepare_data(data):
    """Ensure data is float64 and has proper endianness.

    Note: this is largely aimed at working around a Mayavi bug.

    """
    data = data.copy()
    data = data.astype(np.float64)
    if data.dtype.byteorder == '>':
        data.byteswap(True)
    return data


def _force_render(figures, backend):
    """Ensure plots are updated before properties are used"""
    if not isinstance(figures, list):
        figures = [[figures]]
    for ff in figures:
        for f in ff:
            f.render()
            mlab.draw(figure=f)
    if backend == 'TraitsUI':
        from pyface.api import GUI
        _gui = GUI()
        orig_val = _gui.busy
        _gui.set_busy(busy=True)
        _gui.process_events()
        _gui.set_busy(busy=orig_val)
        _gui.process_events()


def _make_viewer(figure, n_row, n_col, title, scene_size, offscreen,
                 interaction='trackball'):
    """Triage viewer creation

    If n_row == n_col == 1, then we can use a Mayavi figure, which
    generally guarantees that things will be drawn before control
    is returned to the command line. With the multi-view, TraitsUI
    unfortunately has no such support, so we only use it if needed.
    """
    if figure is None:
        # spawn scenes
        h, w = scene_size
        if offscreen is True:
            orig_val = mlab.options.offscreen
            mlab.options.offscreen = True
            with warnings.catch_warnings(record=True):  # traits
                figures = [[mlab.figure(size=(h / n_row, w / n_col))
                            for _ in range(n_col)] for __ in range(n_row)]
            mlab.options.offscreen = orig_val
            _v = None
        else:
            # Triage: don't make TraitsUI if we don't have to
            if n_row == 1 and n_col == 1:
                with warnings.catch_warnings(record=True):  # traits
                    figure = mlab.figure(title, size=(w, h))
                mlab.clf(figure)
                figures = [[figure]]
                _v = None
            else:
                window = _MlabGenerator(n_row, n_col, w, h, title)
                figures, _v = window._get_figs_view()
            if interaction == 'terrain':  # "trackball" is default
                for figure in figures:
                    for f in figure:
                        f.scene.interactor.interactor_style = \
                            tvtk.InteractorStyleTerrain()
    else:
        if isinstance(figure, int):  # use figure with specified id
            figure = [mlab.figure(figure, size=scene_size)]
        elif isinstance(figure, tuple):
            figure = list(figure)
        elif not isinstance(figure, list):
            figure = [figure]
        if not all(isinstance(f, Scene) for f in figure):
            raise TypeError('figure must be a mayavi scene or list of scenes')
        if not len(figure) == n_row * n_col:
            raise ValueError('For the requested view, figure must be a '
                             'list or tuple with exactly %i elements, '
                             'not %i' % (n_row * n_col, len(figure)))
        _v = None
        figures = [figure[slice(ri * n_col, (ri + 1) * n_col)]
                   for ri in range(n_row)]
    return figures, _v


class _MlabGenerator(HasTraits):
    """TraitsUI mlab figure generator"""
    from traitsui.api import View
    view = Instance(View)

    def __init__(self, n_row, n_col, width, height, title, **traits):
        HasTraits.__init__(self, **traits)
        self.mlab_names = []
        self.n_row = n_row
        self.n_col = n_col
        self.width = width
        self.height = height
        for fi in range(n_row * n_col):
            name = 'mlab_view%03g' % fi
            self.mlab_names.append(name)
            self.add_trait(name, Instance(MlabSceneModel, ()))
        self.view = self._get_gen_view()
        self._v = self.edit_traits(view=self.view)
        self._v.title = title

    def _get_figs_view(self):
        figures = []
        ind = 0
        for ri in range(self.n_row):
            rfigs = []
            for ci in range(self.n_col):
                x = getattr(self, self.mlab_names[ind])
                rfigs.append(x.mayavi_scene)
                ind += 1
            figures.append(rfigs)
        return figures, self._v

    def _get_gen_view(self):
        from traitsui.api import (View, Item, VGroup, HGroup)
        ind = 0
        va = []
        for ri in range(self.n_row):
            ha = []
            for ci in range(self.n_col):
                ha += [Item(name=self.mlab_names[ind], style='custom',
                            resizable=True, show_label=False,
                            editor=SceneEditor(scene_class=MayaviScene))]
                ind += 1
            va += [HGroup(*ha)]
        view = View(VGroup(*va), resizable=True,
                    height=self.height, width=self.width)
        return view


class Brain(object):
    """Class for visualizing a brain using multiple views in mlab

    Parameters
    ----------
    subject_id : str
        subject name in Freesurfer subjects dir
    hemi : str
        hemisphere id (ie 'lh', 'rh', 'both', or 'split'). In the case
        of 'both', both hemispheres are shown in the same window.
        In the case of 'split' hemispheres are displayed side-by-side
        in different viewing panes.
    surf : str
        freesurfer surface mesh name (ie 'white', 'inflated', etc.)
    title : str
        title for the window
    cortex : str, tuple, dict, or None
        Specifies how the cortical surface is rendered. Options:

            1. The name of one of the preset cortex styles:
               ``'classic'`` (default), ``'high_contrast'``,
               ``'low_contrast'``, or ``'bone'``.
            2. A color-like argument to render the cortex as a single
               color, e.g. ``'red'`` or ``(0.1, 0.4, 1.)``. Setting
               this to ``None`` is equivalent to ``(0.5, 0.5, 0.5)``.
            3. The name of a colormap used to render binarized
               curvature values, e.g., ``Grays``.
            4. A list of colors used to render binarized curvature
               values. Only the first and last colors are used. E.g.,
               ['red', 'blue'] or [(1, 0, 0), (0, 0, 1)].
            5. A container with four entries for colormap (string
               specifiying the name of a colormap), vmin (float
               specifying the minimum value for the colormap), vmax
               (float specifying the maximum value for the colormap),
               and reverse (bool specifying whether the colormap
               should be reversed. E.g., ``('Greys', -1, 2, False)``.
            6. A dict of keyword arguments that is passed on to the
               call to surface.
    alpha : float in [0, 1]
        Alpha level to control opacity of the cortical surface.
    size : float or pair of floats
        the size of the window, in pixels. can be one number to specify
        a square window, or the (width, height) of a rectangular window.
    background, foreground : matplotlib colors
        color of the background and foreground of the display window
    figure : list of mayavi.core.scene.Scene | None | int
        If None (default), a new window will be created with the appropriate
        views. For single view plots, the figure can be specified as int to
        retrieve the corresponding Mayavi window.
    subjects_dir : str | None
        If not None, this directory will be used as the subjects directory
        instead of the value set using the SUBJECTS_DIR environment
        variable.
    views : list | str
        views to use
    offset : bool
        If True, aligs origin with medial wall. Useful for viewing inflated
        surface where hemispheres typically overlap (Default: True)
    show_toolbar : bool
        If True, toolbars will be shown for each view.
    offscreen : bool
        If True, rendering will be done offscreen (not shown). Useful
        mostly for generating images or screenshots, but can be buggy.
        Use at your own risk.
    interaction : str
        Can be "trackball" (default) or "terrain", i.e. a turntable-style
        camera.

    Attributes
    ----------
    annot : list
        List of annotations.
    brains : list
        List of the underlying brain instances.
    contour : list
        List of the contours.
    foci : foci
        The foci.
    labels : dict
        The labels.
    overlays : dict
        The overlays.
    texts : dict
        The text objects.
    """
    def __init__(self, subject_id, hemi, surf, title=None,
                 cortex="classic", alpha=1.0, size=800, background="black",
                 foreground="white", figure=None, subjects_dir=None,
                 views=['lat'], offset=True, show_toolbar=False,
                 offscreen=False, interaction='trackball',
                 config_opts=None, curv=None):

        # Keep backwards compatability
        if config_opts is not None:
            msg = ("The `config_opts` dict has been deprecated and will "
                   "be removed in future versions. You should update your "
                   "code and pass these options directly to the `Brain` "
                   "constructor.")
            warn(msg, DeprecationWarning)
            cortex = config_opts.get("cortex", cortex)
            background = config_opts.get("background", background)
            foreground = config_opts.get("foreground", foreground)

            size = config_opts.get("size", size)
            width = config_opts.get("width", size)
            height = config_opts.get("height", size)
            size = (width, height)
        # Keep backwards compatability
        if curv is not None:
            msg = ("The `curv` keyword has been deprecated and will "
                   "be removed in future versions. You should update your "
                   "code and use the `cortex` keyword to specify how the "
                   "brain surface is rendered. Setting `cortex` to `None` "
                   "will reproduce the previous behavior when `curv` was "
                   "set to `False`. To emulate the previous behavior for "
                   "cases where `curv` was set to `True`, simply omit it.")
            warn(msg, DeprecationWarning)
            if not curv:
                cortex = None

        if not isinstance(interaction, string_types) or \
                interaction not in ('trackball', 'terrain'):
            raise ValueError('interaction must be "trackball" or "terrain", '
                             'got "%s"' % (interaction,))
        col_dict = dict(lh=1, rh=1, both=1, split=2)
        n_col = col_dict[hemi]
        if hemi not in col_dict.keys():
            raise ValueError('hemi must be one of [%s], not %s'
                             % (', '.join(col_dict.keys()), hemi))
        # Get the subjects directory from parameter or env. var
        subjects_dir = _get_subjects_dir(subjects_dir=subjects_dir)

        self._hemi = hemi
        if title is None:
            title = subject_id
        self.subject_id = subject_id

        if not isinstance(views, list):
            views = [views]
        n_row = len(views)

        # load geometry for one or both hemispheres as necessary
        offset = None if (not offset or hemi != 'both') else 0.0
        self.geo = dict()
        if hemi in ['split', 'both']:
            geo_hemis = ['lh', 'rh']
        elif hemi == 'lh':
            geo_hemis = ['lh']
        elif hemi == 'rh':
            geo_hemis = ['rh']
        else:
            raise ValueError('bad hemi value')
        geo_kwargs, geo_reverse, geo_curv = self._get_geo_params(cortex, alpha)
        for h in geo_hemis:
            # Initialize a Surface object as the geometry
            geo = Surface(subject_id, h, surf, subjects_dir, offset)
            # Load in the geometry and (maybe) curvature
            geo.load_geometry()
            if geo_curv:
                geo.load_curvature()
            self.geo[h] = geo

        # deal with making figures
        self._set_window_properties(size, background, foreground)
        figures, _v = _make_viewer(figure, n_row, n_col, title,
                                   self._scene_size, offscreen,
                                   interaction)
        self._figures = figures
        self._v = _v
        self._window_backend = 'Mayavi' if self._v is None else 'TraitsUI'
        for ff in self._figures:
            for f in ff:
                if f.scene is not None:
                    f.scene.background = self._bg_color
                    f.scene.foreground = self._fg_color

        # force rendering so scene.lights exists
        _force_render(self._figures, self._window_backend)
        self.toggle_toolbars(show_toolbar)
        _force_render(self._figures, self._window_backend)
        self._toggle_render(False)

        # fill figures with brains
        kwargs = dict(geo_curv=geo_curv, geo_kwargs=geo_kwargs,
                      geo_reverse=geo_reverse, subjects_dir=subjects_dir,
                      bg_color=self._bg_color)
        brains = []
        brain_matrix = []
        for ri, view in enumerate(views):
            brain_row = []
            for hi, h in enumerate(['lh', 'rh']):
                if not (hemi in ['lh', 'rh'] and h != hemi):
                    ci = hi if hemi == 'split' else 0
                    kwargs['hemi'] = h
                    kwargs['geo'] = self.geo[h]
                    kwargs['figure'] = figures[ri][ci]
                    kwargs['backend'] = self._window_backend
                    brain = _Hemisphere(subject_id, **kwargs)
                    brain.show_view(view)
                    brains += [dict(row=ri, col=ci, brain=brain, hemi=h)]
                    brain_row += [brain]
            brain_matrix += [brain_row]
        self._toggle_render(True)
        self._original_views = views
        self._brain_list = brains
        for brain in self._brain_list:
            brain['brain']._orient_lights()
        self.brains = [b['brain'] for b in brains]
        self.brain_matrix = np.array(brain_matrix)
        self.subjects_dir = subjects_dir
        self.surf = surf
        # Initialize the overlay and label dictionaries
        self.foci_dict = dict()
        self._label_dicts = dict()
        self.overlays_dict = dict()
        self.contour_list = []
        self.morphometry_list = []
        self.annot_list = []
        self._data_dicts = dict(lh=[], rh=[])
        # note that texts gets treated differently
        self.texts_dict = dict()
        self._times = None
        self.n_times = None

    @property
    def data_dict(self):
        """For backwards compatibility"""
        lh_list = self._data_dicts['lh']
        rh_list = self._data_dicts['rh']
        return dict(lh=lh_list[-1] if lh_list else None,
                    rh=rh_list[-1] if rh_list else None)

    @property
    def labels_dict(self):
        """For backwards compatibility"""
        return {key: data['surfaces'] for key, data in
                self._label_dicts.items()}

    ###########################################################################
    # HELPERS
    def _toggle_render(self, state, views=None):
        """Turn rendering on (True) or off (False)"""
        figs = [fig for fig_row in self._figures for fig in fig_row]
        if views is None:
            views = [None] * len(figs)
        for vi, (_f, view) in enumerate(zip(figs, views)):
            # Testing backend doesn't have these options
            if mlab.options.backend == 'test':
                continue

            if state is False and view is None:
                views[vi] = (mlab.view(figure=_f),
                             _f.scene.camera.parallel_scale)

            _f.scene.disable_render = not state

            if state is True and view is not None:
                mlab.draw(figure=_f)
                with warnings.catch_warnings(record=True):  # traits focalpoint
                    mlab.view(*view[0], figure=_f)
                _f.scene.camera.parallel_scale = view[1]
        # let's do the ugly force draw
        if state is True:
            _force_render(self._figures, self._window_backend)
        return views

    def _set_window_properties(self, size, background, foreground):
        """Set window properties that are used elsewhere."""
        # old option "size" sets both width and height
        from matplotlib.colors import colorConverter
        try:
            width, height = size
        except (TypeError, ValueError):
            width, height = size, size
        self._scene_size = height, width

        bg_color_rgb = colorConverter.to_rgb(background)
        self._bg_color = bg_color_rgb

        fg_color_rgb = colorConverter.to_rgb(foreground)
        self._fg_color = fg_color_rgb

    def _get_geo_params(self, cortex, alpha=1.0):
        """Return keyword arguments and other parameters for surface
        rendering.

        Parameters
        ----------
        cortex : {str, tuple, dict, None}
            Can be set to: (1) the name of one of the preset cortex
            styles ('classic', 'high_contrast', 'low_contrast', or
            'bone'), (2) the name of a colormap, (3) a tuple with
            four entries for (colormap, vmin, vmax, reverse)
            indicating the name of the colormap, the min and max
            values respectively and whether or not the colormap should
            be reversed, (4) a valid color specification (such as a
            3-tuple with RGB values or a valid color name), or (5) a
            dictionary of keyword arguments that is passed on to the
            call to surface. If set to None, color is set to (0.5,
            0.5, 0.5).
        alpha : float in [0, 1]
            Alpha level to control opacity of the cortical surface.

        Returns
        -------
        kwargs : dict
            Dictionary with keyword arguments to be used for surface
            rendering. For colormaps, keys are ['colormap', 'vmin',
            'vmax', 'alpha'] to specify the name, minimum, maximum,
            and alpha transparency of the colormap respectively. For
            colors, keys are ['color', 'alpha'] to specify the name
            and alpha transparency of the color respectively.
        reverse : boolean
            Boolean indicating whether a colormap should be
            reversed. Set to False if a color (rather than a colormap)
            is specified.
        curv : boolean
            Boolean indicating whether curv file is loaded and binary
            curvature is displayed.

        """
        from matplotlib.colors import colorConverter
        colormap_map = dict(classic=(dict(colormap="Greys",
                                          vmin=-1, vmax=2,
                                          opacity=alpha), False, True),
                            high_contrast=(dict(colormap="Greys",
                                                vmin=-.1, vmax=1.3,
                                                opacity=alpha), False, True),
                            low_contrast=(dict(colormap="Greys",
                                               vmin=-5, vmax=5,
                                               opacity=alpha), False, True),
                            bone=(dict(colormap="bone",
                                       vmin=-.2, vmax=2,
                                       opacity=alpha), True, True))
        if isinstance(cortex, dict):
            if 'opacity' not in cortex:
                cortex['opacity'] = alpha
            if 'colormap' in cortex:
                if 'vmin' not in cortex:
                    cortex['vmin'] = -1
                if 'vmax' not in cortex:
                    cortex['vmax'] = 2
            geo_params = cortex, False, True
        elif isinstance(cortex, string_types):
            if cortex in colormap_map:
                geo_params = colormap_map[cortex]
            elif cortex in lut_manager.lut_mode_list():
                geo_params = dict(colormap=cortex, vmin=-1, vmax=2,
                                  opacity=alpha), False, True
            else:
                try:
                    color = colorConverter.to_rgb(cortex)
                    geo_params = dict(color=color, opacity=alpha), False, False
                except ValueError:
                    geo_params = cortex, False, True
        # check for None before checking len:
        elif cortex is None:
            geo_params = dict(color=(0.5, 0.5, 0.5),
                              opacity=alpha), False, False
        # Test for 4-tuple specifying colormap parameters. Need to
        # avoid 4 letter strings and 4-tuples not specifying a
        # colormap name in the first position (color can be specified
        # as RGBA tuple, but the A value will be dropped by to_rgb()):
        elif (len(cortex) == 4) and (isinstance(cortex[0], string_types)):
            geo_params = dict(colormap=cortex[0], vmin=cortex[1],
                              vmax=cortex[2], opacity=alpha), cortex[3], True
        else:
            try:  # check if it's a non-string color specification
                color = colorConverter.to_rgb(cortex)
                geo_params = dict(color=color, opacity=alpha), False, False
            except ValueError:
                try:
                    lut = create_color_lut(cortex)
                    geo_params = dict(colormap="Greys", opacity=alpha,
                                      lut=lut), False, True
                except ValueError:
                    geo_params = cortex, False, True
        return geo_params

    def get_data_properties(self):
        """ Get properties of the data shown

        Returns
        -------
        props : dict
            Dictionary with data properties

            props["fmin"] : minimum colormap
            props["fmid"] : midpoint colormap
            props["fmax"] : maximum colormap
            props["transparent"] : lower part of colormap transparent?
            props["time"] : time points
            props["time_idx"] : current time index
            props["smoothing_steps"] : number of smoothing steps
        """
        props = dict()
        keys = ['fmin', 'fmid', 'fmax', 'transparent', 'time', 'time_idx',
                'smoothing_steps']
        try:
            if self.data_dict['lh'] is not None:
                hemi = 'lh'
            else:
                hemi = 'rh'
            for key in keys:
                props[key] = self.data_dict[hemi][key]
        except KeyError:
            # The user has not added any data
            for key in keys:
                props[key] = 0
        return props

    def toggle_toolbars(self, show=None):
        """Toggle toolbar display

        Parameters
        ----------
        show : bool | None
            If None, the state is toggled. If True, the toolbar will
            be shown, if False, hidden.
        """
        # don't do anything if testing is on
        if self._figures[0][0].scene is not None:
            # this may not work if QT is not the backend (?), or in testing
            if hasattr(self._figures[0][0].scene, 'scene_editor'):
                # Within TraitsUI
                bars = [f.scene.scene_editor._tool_bar
                        for ff in self._figures for f in ff]
            else:
                # Mayavi figure
                bars = [f.scene._tool_bar for ff in self._figures for f in ff]

            if show is None:
                if hasattr(bars[0], 'isVisible'):
                    # QT4
                    show = not bars[0].isVisible()
                elif hasattr(bars[0], 'Shown'):
                    # WX
                    show = not bars[0].Shown()
            for bar in bars:
                if hasattr(bar, 'setVisible'):
                    bar.setVisible(show)
                elif hasattr(bar, 'Show'):
                    bar.Show(show)

    def _get_one_brain(self, d, name):
        """Helper for various properties"""
        if len(self.brains) > 1:
            raise ValueError('Cannot access brain.%s when more than '
                             'one view is plotted. Use brain.brain_matrix '
                             'or brain.brains.' % name)
        if isinstance(d, dict):
            out = dict()
            for key, value in d.items():
                out[key] = value[0]
        else:
            out = d[0]
        return out

    @property
    def overlays(self):
        return self._get_one_brain(self.overlays_dict, 'overlays')

    @property
    def foci(self):
        return self._get_one_brain(self.foci_dict, 'foci')

    @property
    def labels(self):
        return self._get_one_brain(self.labels_dict, 'labels')

    @property
    def contour(self):
        return self._get_one_brain(self.contour_list, 'contour')

    @property
    def annot(self):
        return self._get_one_brain(self.annot_list, 'annot')

    @property
    def texts(self):
        self._get_one_brain([[]], 'texts')
        out = dict()
        for key, val in self.texts_dict.iteritems():
            out[key] = val['text']
        return out

    @property
    def data(self):
        self._get_one_brain([[]], 'data')
        if self.data_dict['lh'] is not None:
            data = self.data_dict['lh'].copy()
        else:
            data = self.data_dict['rh'].copy()
        if 'colorbars' in data:
            data['colorbar'] = data['colorbars'][0]
        return data

    def _check_hemi(self, hemi):
        """Check for safe single-hemi input, returns str"""
        if hemi is None:
            if self._hemi not in ['lh', 'rh']:
                raise ValueError('hemi must not be None when both '
                                 'hemispheres are displayed')
            else:
                hemi = self._hemi
        elif hemi not in ['lh', 'rh']:
            extra = ' or None' if self._hemi in ['lh', 'rh'] else ''
            raise ValueError('hemi must be either "lh" or "rh"' + extra)
        return hemi

    def _check_hemis(self, hemi):
        """Check for safe dual or single-hemi input, returns list"""
        if hemi is None:
            if self._hemi not in ['lh', 'rh']:
                hemi = ['lh', 'rh']
            else:
                hemi = [self._hemi]
        elif hemi not in ['lh', 'rh']:
            extra = ' or None' if self._hemi in ['lh', 'rh'] else ''
            raise ValueError('hemi must be either "lh" or "rh"' + extra)
        else:
            hemi = [hemi]
        return hemi

    def _read_scalar_data(self, source, hemi, name=None, cast=True):
        """Load in scalar data from an image stored in a file or an array

        Parameters
        ----------
        source : str or numpy array
            path to scalar data file or a numpy array
        name : str or None, optional
            name for the overlay in the internal dictionary
        cast : bool, optional
            either to cast float data into 64bit datatype as a
            workaround. cast=True can fix a rendering problem with
            certain versions of Mayavi

        Returns
        -------
        scalar_data : numpy array
            flat numpy array of scalar data
        name : str
            if no name was provided, deduces the name if filename was given
            as a source
        """
        # If source is a string, try to load a file
        if isinstance(source, string_types):
            if name is None:
                basename = os.path.basename(source)
                if basename.endswith(".gz"):
                    basename = basename[:-3]
                if basename.startswith("%s." % hemi):
                    basename = basename[3:]
                name = os.path.splitext(basename)[0]
            scalar_data = io.read_scalar_data(source)
        else:
            # Can't think of a good way to check that this will work nicely
            scalar_data = source

        if cast:
            if (scalar_data.dtype.char == 'f' and
                    scalar_data.dtype.itemsize < 8):
                scalar_data = scalar_data.astype(np.float)

        return scalar_data, name

    def _get_display_range(self, scalar_data, min, max, sign):
        if scalar_data.min() >= 0:
            sign = "pos"
        elif scalar_data.max() <= 0:
            sign = "neg"

        # Get data with a range that will make sense for automatic thresholding
        if sign == "neg":
            range_data = np.abs(scalar_data[np.where(scalar_data < 0)])
        elif sign == "pos":
            range_data = scalar_data[np.where(scalar_data > 0)]
        else:
            range_data = np.abs(scalar_data)

        # Get a numeric value for the scalar minimum
        if min is None:
            min = "robust_min"
        if min == "robust_min":
            min = np.percentile(range_data, 2)
        elif min == "actual_min":
            min = range_data.min()

        # Get a numeric value for the scalar maximum
        if max is None:
            max = "robust_max"
        if max == "robust_max":
            max = np.percentile(scalar_data, 98)
        elif max == "actual_max":
            max = range_data.max()

        return min, max

    def _iter_time(self, time_idx, interpolation):
        """Iterate through time points, then reset to current time

        Parameters
        ----------
        time_idx : array_like
            Time point indexes through which to iterate.
        interpolation : str
            Interpolation method (``scipy.interpolate.interp1d`` parameter,
            one of 'linear' | 'nearest' | 'zero' | 'slinear' | 'quadratic' |
            'cubic'). Interpolation is only used for non-integer indexes.

        Yields
        ------
        idx : int | float
            Current index.

        Notes
        -----
        Used by movie and image sequence saving functions.
        """
        current_time_idx = self.data_time_index
        for idx in time_idx:
            self.set_data_time_index(idx, interpolation)
            yield idx

        # Restore original time index
        self.set_data_time_index(current_time_idx)

    ###########################################################################
    # ADDING DATA PLOTS
    def add_overlay(self, source, min=2, max="robust_max", sign="abs",
                    name=None, hemi=None):
        """Add an overlay to the overlay dict from a file or array.

        Parameters
        ----------
        source : str or numpy array
            path to the overlay file or numpy array with data
        min : float
            threshold for overlay display
        max : float
            saturation point for overlay display
        sign : {'abs' | 'pos' | 'neg'}
            whether positive, negative, or both values should be displayed
        name : str
            name for the overlay in the internal dictionary
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, an error will
            be thrown.
        """
        hemi = self._check_hemi(hemi)
        # load data here
        scalar_data, name = self._read_scalar_data(source, hemi, name=name)
        min, max = self._get_display_range(scalar_data, min, max, sign)
        if sign not in ["abs", "pos", "neg"]:
            raise ValueError("Overlay sign must be 'abs', 'pos', or 'neg'")
        old = OverlayData(scalar_data, min, max, sign)
        ol = []
        views = self._toggle_render(False)
        for brain in self._brain_list:
            if brain['hemi'] == hemi:
                ol.append(brain['brain'].add_overlay(old))
        if name in self.overlays_dict:
            name = "%s%d" % (name, len(self.overlays_dict) + 1)
        self.overlays_dict[name] = ol
        self._toggle_render(True, views)

    def add_data(self, array, min=None, max=None, thresh=None,
                 colormap="RdBu_r", alpha=1,
                 vertices=None, smoothing_steps=20, time=None,
                 time_label="time index=%d", colorbar=True,
                 hemi=None, remove_existing=False, time_label_size=14,
                 initial_time=None):
        """Display data from a numpy array on the surface.

        This provides a similar interface to add_overlay, but it displays
        it with a single colormap. It offers more flexibility over the
        colormap, and provides a way to display four dimensional data
        (i.e. a timecourse).

        Note that min sets the low end of the colormap, and is separate
        from thresh (this is a different convention from add_overlay)

        Note: If the data is defined for a subset of vertices (specified
        by the "vertices" parameter), a smoothing method is used to interpolate
        the data onto the high resolution surface. If the data is defined for
        subsampled version of the surface, smoothing_steps can be set to None,
        in which case only as many smoothing steps are applied until the whole
        surface is filled with non-zeros.

        Parameters
        ----------
        array : numpy array
            data array (nvtx vector)
        min : float
            min value in colormap (uses real min if None)
        max : float
            max value in colormap (uses real max if None)
        thresh : None or float
            if not None, values below thresh will not be visible
        colormap : string, list of colors, or array
            name of matplotlib colormap to use, a list of matplotlib colors,
            or a custom look up table (an n x 4 array coded with RBGA values
            between 0 and 255).
        alpha : float in [0, 1]
            alpha level to control opacity
        vertices : numpy array
            vertices for which the data is defined (needed if len(data) < nvtx)
        smoothing_steps : int or None
            number of smoothing steps (smoothing is used if len(data) < nvtx)
            Default : 20
        time : numpy array
            time points in the data array (if data is 2D)
        time_label : str | callable | None
            format of the time label (a format string, a function that maps
            floating point time values to strings, or None for no label)
        colorbar : bool
            whether to add a colorbar to the figure
        hemi : str | None
            If None, it is assumed to belong to the hemisphere being
            shown. If two hemispheres are being shown, an error will
            be thrown.
        remove_existing : bool
            Remove surface added by previous "add_data" call. Useful for
            conserving memory when displaying different data in a loop.
        time_label_size : int
            Font size of the time label (default 14)
        initial_time : float | None
            Time initially shown in the plot. ``None`` to use the first time
            sample (default).
        """
        hemi = self._check_hemi(hemi)
        array = np.asarray(array)

        if min is None:
            min = array.min() if array.size > 0 else 0
        if max is None:
            max = array.max() if array.size > 0 else 0

        # Create smoothing matrix if necessary
        if len(array) < self.geo[hemi].x.shape[0]:
            if vertices is None:
                raise ValueError("len(data) < nvtx: need vertices")
            adj_mat = utils.mesh_edges(self.geo[hemi].faces)
            smooth_mat = utils.smoothing_matrix(vertices, adj_mat,
                                                smoothing_steps)
        else:
            smooth_mat = None

        # Calculate initial data to plot
        if array.ndim == 1:
            array_plot = array
        elif array.ndim == 2:
            array_plot = array[:, 0]
        else:
            raise ValueError("data has to be 1D or 2D")

        if smooth_mat is not None:
            array_plot = smooth_mat * array_plot

        # Copy and byteswap to deal with Mayavi bug
        mlab_plot = _prepare_data(array_plot)

        # Process colormap argument into a lut
        lut = create_color_lut(colormap)
        colormap = "Greys"

        # determine unique data layer ID
        data_dicts = self._data_dicts['lh'] + self._data_dicts['rh']
        if data_dicts:
            layer_id = np.max([data['layer_id'] for data in data_dicts]) + 1
        else:
            layer_id = 0

        data = dict(array=array, smoothing_steps=smoothing_steps,
                    fmin=min, fmid=(min + max) / 2, fmax=max,
                    transparent=False, time=0, time_idx=0,
                    vertices=vertices, smooth_mat=smooth_mat,
                    layer_id=layer_id)

        # clean up existing data
        if remove_existing:
            self.remove_data(hemi)

        # Create time array and add label if 2D
        if array.ndim == 2:
            # check time array
            if time is None:
                time = np.arange(array.shape[1])
            else:
                time = np.asarray(time)
                if time.shape != (array.shape[1],):
                    raise ValueError('time has shape %s, but need shape %s '
                                     '(array.shape[1])' %
                                     (time.shape, (array.shape[1],)))

            if self.n_times is None:
                self.n_times = len(time)
                self._times = time
            elif len(time) != self.n_times:
                raise ValueError("New n_times is different from previous "
                                 "n_times")
            elif not np.array_equal(time, self._times):
                raise ValueError("Not all time values are consistent with "
                                 "previously set times.")

            # initial time
            if initial_time is None:
                initial_time_index = None
            else:
                initial_time_index = self.index_for_time(initial_time)

            # time label
            if isinstance(time_label, string_types):
                time_label_fmt = time_label

                def time_label(x):
                    return time_label_fmt % x
            data["time_label"] = time_label
            data["time"] = time
            data["time_idx"] = 0
            y_txt = 0.05 + 0.05 * bool(colorbar)
        else:
            initial_time_index = None

        surfs = []
        bars = []
        views = self._toggle_render(False)
        for brain in self._brain_list:
            if brain['hemi'] == hemi:
                s, ct, bar = brain['brain'].add_data(
                    array, mlab_plot, min, max, thresh, lut, colormap, alpha,
                    colorbar, layer_id)
                surfs.append(s)
                bars.append(bar)
                if array.ndim == 2 and time_label is not None:
                    self.add_text(0.95, y_txt, time_label(time[0]),
                                  name="time_label", row=brain['row'],
                                  col=brain['col'], font_size=time_label_size,
                                  justification='right')
        data['surfaces'] = surfs
        data['colorbars'] = bars
        data['orig_ctable'] = ct

        self._data_dicts[hemi].append(data)

        if initial_time_index is not None:
            self.set_data_time_index(initial_time_index)
        self._toggle_render(True, views)

    def add_annotation(self, annot, borders=True, alpha=1, hemi=None,
                       remove_existing=True):
        """Add an annotation file.

        Parameters
        ----------
        annot : str | tuple
            Either path to annotation file or annotation name. Alternatively,
            the annotation can be specified as a ``(labels, ctab)`` tuple per
            hemisphere, i.e. ``annot=(labels, ctab)`` for a single hemisphere
            or ``annot=((lh_labels, lh_ctab), (rh_labels, rh_ctab))`` for both
            hemispheres. ``labels`` and ``ctab`` should be arrays as returned
            by :func:`nibabel.freesurfer.read_annot`.
        borders : bool | int
            Show only label borders. If int, specify the number of steps
            (away from the true border) along the cortical mesh to include
            as part of the border definition.
        alpha : float in [0, 1]
            Alpha level to control opacity.
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, data must exist
            for both hemispheres.
        remove_existing : bool
            If True (default), remove old annotations.
        """
        hemis = self._check_hemis(hemi)

        # Figure out where the data is coming from
        if isinstance(annot, string_types):
            if os.path.isfile(annot):
                filepath = annot
                path = os.path.split(filepath)[0]
                file_hemi, annot = os.path.basename(filepath).split('.')[:2]
                if len(hemis) > 1:
                    if annot[:2] == 'lh.':
                        filepaths = [filepath, pjoin(path, 'rh' + annot[2:])]
                    elif annot[:2] == 'rh.':
                        filepaths = [pjoin(path, 'lh' + annot[2:], filepath)]
                    else:
                        raise RuntimeError('To add both hemispheres '
                                           'simultaneously, filename must '
                                           'begin with "lh." or "rh."')
                else:
                    filepaths = [filepath]
            else:
                filepaths = []
                for hemi in hemis:
                    filepath = pjoin(self.subjects_dir,
                                     self.subject_id,
                                     'label',
                                     ".".join([hemi, annot, 'annot']))
                    if not os.path.exists(filepath):
                        raise ValueError('Annotation file %s does not exist'
                                         % filepath)
                    filepaths += [filepath]
            annots = []
            for hemi, filepath in zip(hemis, filepaths):
                # Read in the data
                labels, cmap, _ = nib.freesurfer.read_annot(
                    filepath, orig_ids=True)
                annots.append((labels, cmap))
        else:
            annots = [annot] if len(hemis) == 1 else annot
            annot = 'annotation'

        views = self._toggle_render(False)
        if remove_existing:
            # Get rid of any old annots
            for a in self.annot_list:
                a['brain']._remove_scalar_data(a['array_id'])
            self.annot_list = []

        for hemi, (labels, cmap) in zip(hemis, annots):

            # Maybe zero-out the non-border vertices
            self._to_borders(labels, hemi, borders)

            # Handle null labels properly
            # (tksurfer doesn't use the alpha channel, so sometimes this
            # is set weirdly. For our purposes, it should always be 0.
            # Unless this sometimes causes problems?
            cmap[np.where(cmap[:, 4] == 0), 3] = 0
            if np.any(labels == 0) and not np.any(cmap[:, -1] == 0):
                cmap = np.vstack((cmap, np.zeros(5, int)))

            # Set label ids sensibly
            ord = np.argsort(cmap[:, -1])
            ids = ord[np.searchsorted(cmap[ord, -1], labels)]
            cmap = cmap[:, :4]

            #  Set the alpha level
            alpha_vec = cmap[:, 3]
            alpha_vec[alpha_vec > 0] = alpha * 255

            for brain in self._brain_list:
                if brain['hemi'] == hemi:
                    self.annot_list.append(
                        brain['brain'].add_annotation(annot, ids, cmap))
        self._toggle_render(True, views)

    def add_label(self, label, color=None, alpha=1, scalar_thresh=None,
                  borders=False, hemi=None, subdir=None):
        """Add an ROI label to the image.

        Parameters
        ----------
        label : str | instance of Label
            label filepath or name. Can also be an instance of
            an object with attributes "hemi", "vertices", "name", and
            optionally "color" and "values" (if scalar_thresh is not None).
        color : matplotlib-style color | None
            anything matplotlib accepts: string, RGB, hex, etc. (default
            "crimson")
        alpha : float in [0, 1]
            alpha level to control opacity
        scalar_thresh : None or number
            threshold the label ids using this value in the label
            file's scalar field (i.e. label only vertices with
            scalar >= thresh)
        borders : bool | int
            Show only label borders. If int, specify the number of steps
            (away from the true border) along the cortical mesh to include
            as part of the border definition.
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, an error will
            be thrown.
        subdir : None | str
            If a label is specified as name, subdir can be used to indicate
            that the label file is in a sub-directory of the subject's
            label directory rather than in the label directory itself (e.g.
            for ``$SUBJECTS_DIR/$SUBJECT/label/aparc/lh.cuneus.label``
            ``brain.add_label('cuneus', subdir='aparc')``).

        Notes
        -----
        To remove previously added labels, run Brain.remove_labels().
        """
        if isinstance(label, string_types):
            hemi = self._check_hemi(hemi)
            if color is None:
                color = "crimson"

            if os.path.isfile(label):
                filepath = label
                label_name = os.path.basename(filepath).split('.')[1]
            else:
                label_name = label
                label_fname = ".".join([hemi, label_name, 'label'])
                if subdir is None:
                    filepath = pjoin(self.subjects_dir, self.subject_id,
                                     'label', label_fname)
                else:
                    filepath = pjoin(self.subjects_dir, self.subject_id,
                                     'label', subdir, label_fname)
                if not os.path.exists(filepath):
                    raise ValueError('Label file %s does not exist'
                                     % filepath)
            # Load the label data and create binary overlay
            if scalar_thresh is None:
                ids = nib.freesurfer.read_label(filepath)
            else:
                ids, scalars = nib.freesurfer.read_label(filepath,
                                                         read_scalars=True)
                ids = ids[scalars >= scalar_thresh]
        else:
            # try to extract parameters from label instance
            try:
                hemi = label.hemi
                ids = label.vertices
                if label.name is None:
                    label_name = 'unnamed'
                else:
                    label_name = str(label.name)

                if color is None:
                    if hasattr(label, 'color') and label.color is not None:
                        color = label.color
                    else:
                        color = "crimson"

                if scalar_thresh is not None:
                    scalars = label.values
            except Exception:
                raise ValueError('Label was not a filename (str), and could '
                                 'not be understood as a class. The class '
                                 'must have attributes "hemi", "vertices", '
                                 '"name", and (if scalar_thresh is not None)'
                                 '"values"')
            hemi = self._check_hemi(hemi)

            if scalar_thresh is not None:
                ids = ids[scalars >= scalar_thresh]

        label = np.zeros(self.geo[hemi].coords.shape[0])
        label[ids] = 1

        # make sure we have a unique name
        if label_name in self._label_dicts:
            i = 2
            name = label_name + '_%i'
            while name % i in self._label_dicts:
                i += 1
            label_name = name % i

        self._to_borders(label, hemi, borders, restrict_idx=ids)

        # make a list of all the plotted labels
        surfaces = []
        array_ids = []
        views = self._toggle_render(False)
        for brain in self.brains:
            if brain.hemi == hemi:
                array_id, surf = brain.add_label(label, label_name, color,
                                                 alpha)
                surfaces.append(surf)
                array_ids.append((brain, array_id))
        self._label_dicts[label_name] = {'surfaces': surfaces,
                                         'array_ids': array_ids}
        self._toggle_render(True, views)

    def _to_borders(self, label, hemi, borders, restrict_idx=None):
        """Helper to potentially convert a label/parc to borders"""
        if not isinstance(borders, (bool, int)) or borders < 0:
            raise ValueError('borders must be a bool or positive integer')
        if borders:
            n_vertices = label.size
            edges = utils.mesh_edges(self.geo[hemi].faces)
            border_edges = label[edges.row] != label[edges.col]
            show = np.zeros(n_vertices, dtype=np.int)
            keep_idx = np.unique(edges.row[border_edges])
            if isinstance(borders, int):
                for _ in range(borders):
                    keep_idx = np.in1d(self.geo[hemi].faces.ravel(), keep_idx)
                    keep_idx.shape = self.geo[hemi].faces.shape
                    keep_idx = self.geo[hemi].faces[np.any(keep_idx, axis=1)]
                    keep_idx = np.unique(keep_idx)
                if restrict_idx is not None:
                    keep_idx = keep_idx[np.in1d(keep_idx, restrict_idx)]
            show[keep_idx] = 1
            label *= show

    def remove_data(self, hemi=None):
        """Remove data shown with ``Brain.add_data()``.

        Parameters
        ----------
        hemi : str | None
            Hemisphere from which to remove data (default is all shown
            hemispheres).
        """
        hemis = self._check_hemis(hemi)
        for hemi in hemis:
            for brain in self.brains:
                if brain.hemi == hemi:
                    for data in self._data_dicts[hemi]:
                        brain.remove_data(data['layer_id'])
            self._data_dicts[hemi] = []

        # if no data is left, reset time properties
        if all(len(brain.data) == 0 for brain in self.brains):
            self.n_times = self._times = None

    def remove_labels(self, labels=None, hemi=None):
        """Remove one or more previously added labels from the image.

        Parameters
        ----------
        labels : None | str | list of str
            Labels to remove. Can be a string naming a single label, or None to
            remove all labels. Possible names can be found in the Brain.labels
            attribute.
        hemi : None
            Deprecated parameter, do not use.
        """
        if hemi is not None:
            warn("The `hemi` parameter to Brain.remove_labels() has no effect "
                 "and will be removed in PySurfer 0.9", DeprecationWarning)

        if labels is None:
            labels_ = self._label_dicts.keys()
        else:
            labels_ = [labels] if isinstance(labels, str) else labels
            missing = [key for key in labels_ if key not in self._label_dicts]
            if missing:
                raise ValueError("labels=%r contains unknown labels: %s" %
                                 (labels, ', '.join(map(repr, missing))))

        for key in labels_:
            data = self._label_dicts.pop(key)
            for brain, array_id in data['array_ids']:
                brain._remove_scalar_data(array_id)

    def add_morphometry(self, measure, grayscale=False, hemi=None,
                        remove_existing=True, colormap=None,
                        min=None, max=None, colorbar=True):
        """Add a morphometry overlay to the image.

        Parameters
        ----------
        measure : {'area' | 'curv' | 'jacobian_white' | 'sulc' | 'thickness'}
            which measure to load
        grayscale : bool
            whether to load the overlay with a grayscale colormap
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, data must exist
            for both hemispheres.
        remove_existing : bool
            If True (default), remove old annotations.
        colormap : str
            Mayavi colormap name, or None to use a sensible default.
        min, max : floats
            Endpoints for the colormap; if not provided the robust range
            of the data is used.
        colorbar : bool
            If True, show a colorbar corresponding to the overlay data.

        """
        hemis = self._check_hemis(hemi)
        morph_files = []
        for hemi in hemis:
            # Find the source data
            surf_dir = pjoin(self.subjects_dir, self.subject_id, 'surf')
            morph_file = pjoin(surf_dir, '.'.join([hemi, measure]))
            if not os.path.exists(morph_file):
                raise ValueError(
                    'Could not find %s in subject directory' % morph_file)
            morph_files += [morph_file]

        views = self._toggle_render(False)
        if remove_existing is True:
            # Get rid of any old overlays
            for m in self.morphometry_list:
                if m["colorbar"] is not None:
                    m['colorbar'].visible = False
                m['brain']._remove_scalar_data(m['array_id'])
            self.morphometry_list = []

        for hemi, morph_file in zip(hemis, morph_files):

            if colormap is None:
                # Preset colormaps
                if grayscale:
                    colormap = "gray"
                else:
                    colormap = dict(area="pink",
                                    curv="RdBu",
                                    jacobian_white="pink",
                                    sulc="RdBu",
                                    thickness="pink")[measure]

            # Read in the morphometric data
            morph_data = nib.freesurfer.read_morph_data(morph_file)

            # Get a cortex mask for robust range
            self.geo[hemi].load_label("cortex")
            ctx_idx = self.geo[hemi].labels["cortex"]

            # Get the display range
            min_default, max_default = np.percentile(morph_data[ctx_idx],
                                                     [2, 98])
            if min is None:
                min = min_default
            if max is None:
                max = max_default

            # Use appropriate values for bivariate measures
            if measure in ["curv", "sulc"]:
                lim = np.max([abs(min), abs(max)])
                min, max = -lim, lim

            # Set up the Mayavi pipeline
            morph_data = _prepare_data(morph_data)

            for brain in self.brains:
                if brain.hemi == hemi:
                    self.morphometry_list.append(brain.add_morphometry(
                        morph_data, colormap, measure, min, max, colorbar))
        self._toggle_render(True, views)

    def add_foci(self, coords, coords_as_verts=False, map_surface=None,
                 scale_factor=1, color="white", alpha=1, name=None,
                 hemi=None):
        """Add spherical foci, possibly mapping to displayed surf.

        The foci spheres can be displayed at the coordinates given, or
        mapped through a surface geometry. In other words, coordinates
        from a volume-based analysis in MNI space can be displayed on an
        inflated average surface by finding the closest vertex on the
        white surface and mapping to that vertex on the inflated mesh.

        Parameters
        ----------
        coords : numpy array
            x, y, z coordinates in stereotaxic space or array of vertex ids
        coords_as_verts : bool
            whether the coords parameter should be interpreted as vertex ids
        map_surface : Freesurfer surf or None
            surface to map coordinates through, or None to use raw coords
        scale_factor : int
            controls the size of the foci spheres
        color : matplotlib color code
            HTML name, RBG tuple, or hex code
        alpha : float in [0, 1]
            opacity of focus gylphs
        name : str
            internal name to use
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, an error will
            be thrown.
        """
        from matplotlib.colors import colorConverter
        hemi = self._check_hemi(hemi)

        # Figure out how to interpret the first parameter
        if coords_as_verts:
            coords = self.geo[hemi].coords[coords]
            map_surface = None

        # Possibly map the foci coords through a surface
        if map_surface is None:
            foci_coords = np.atleast_2d(coords)
        else:
            foci_surf = Surface(self.subject_id, hemi, map_surface,
                                subjects_dir=self.subjects_dir)
            foci_surf.load_geometry()
            foci_vtxs = utils.find_closest_vertices(foci_surf.coords, coords)
            foci_coords = self.geo[hemi].coords[foci_vtxs]

        # Get a unique name (maybe should take this approach elsewhere)
        if name is None:
            name = "foci_%d" % (len(self.foci_dict) + 1)

        # Convert the color code
        if not isinstance(color, tuple):
            color = colorConverter.to_rgb(color)

        views = self._toggle_render(False)
        fl = []
        for brain in self._brain_list:
            if brain['hemi'] == hemi:
                fl.append(brain['brain'].add_foci(foci_coords, scale_factor,
                                                  color, alpha, name))
        self.foci_dict[name] = fl
        self._toggle_render(True, views)

    def add_contour_overlay(self, source, min=None, max=None,
                            n_contours=7, line_width=1.5, colormap="YlOrRd_r",
                            hemi=None, remove_existing=True, colorbar=True):
        """Add a topographic contour overlay of the positive data.

        Note: This visualization will look best when using the "low_contrast"
        cortical curvature colorscheme.

        Parameters
        ----------
        source : str or array
            path to the overlay file or numpy array
        min : float
            threshold for overlay display
        max : float
            saturation point for overlay display
        n_contours : int
            number of contours to use in the display
        line_width : float
            width of contour lines
        colormap : string, list of colors, or array
            name of matplotlib colormap to use, a list of matplotlib colors,
            or a custom look up table (an n x 4 array coded with RBGA values
            between 0 and 255).
        hemi : str | None
            If None, it is assumed to belong to the hemipshere being
            shown. If two hemispheres are being shown, an error will
            be thrown.
        remove_existing : bool
            If there is an existing contour overlay, remove it before plotting.
        colorbar : bool
            If True, show the colorbar for the scalar value.

        """
        hemi = self._check_hemi(hemi)

        # Read the scalar data
        scalar_data, _ = self._read_scalar_data(source, hemi)
        min, max = self._get_display_range(scalar_data, min, max, "pos")

        # Deal with Mayavi bug
        scalar_data = _prepare_data(scalar_data)

        # Maybe get rid of an old overlay
        if remove_existing:
            for c in self.contour_list:
                if c['colorbar'] is not None:
                    c['colorbar'].visible = False
                c['brain']._remove_scalar_data(c['array_id'])
            self.contour_list = []

        # Process colormap argument into a lut
        lut = create_color_lut(colormap)

        views = self._toggle_render(False)
        for brain in self.brains:
            if brain.hemi == hemi:
                self.contour_list.append(brain.add_contour_overlay(
                    scalar_data, min, max, n_contours, line_width, lut,
                    colorbar))
        self._toggle_render(True, views)

    def add_text(self, x, y, text, name, color=None, opacity=1.0,
                 row=-1, col=-1, font_size=None, justification=None):
        """ Add a text to the visualization

        Parameters
        ----------
        x : Float
            x coordinate
        y : Float
            y coordinate
        text : str
            Text to add
        name : str
            Name of the text (text label can be updated using update_text())
        color : Tuple
            Color of the text. Default: (1, 1, 1)
        opacity : Float
            Opacity of the text. Default: 1.0
        row : int
            Row index of which brain to use
        col : int
            Column index of which brain to use
        """
        if name in self.texts_dict:
            self.texts_dict[name]['text'].remove()
        text = self.brain_matrix[row, col].add_text(x, y, text,
                                                    name, color, opacity)
        self.texts_dict[name] = dict(row=row, col=col, text=text)
        if font_size is not None:
            text.property.font_size = font_size
            text.actor.text_scale_mode = 'viewport'
        if justification is not None:
            text.property.justification = justification

    def update_text(self, text, name, row=-1, col=-1):
        """Update text label

        Parameters
        ----------
        text : str
            New text for label
        name : str
            Name of text label
        """
        if name not in self.texts_dict:
            raise KeyError('text name "%s" unknown' % name)
        self.texts_dict[name]['text'].text = text

    ###########################################################################
    # DATA SCALING / DISPLAY
    def reset_view(self):
        """Orient camera to display original view
        """
        for view, brain in zip(self._original_views, self._brain_list):
            brain['brain'].show_view(view)

    def show_view(self, view=None, roll=None, distance=None, row=-1, col=-1):
        """Orient camera to display view

        Parameters
        ----------
        view : str | dict
            brain surface to view (one of 'lateral', 'medial', 'rostral',
            'caudal', 'dorsal', 'ventral', 'frontal', 'parietal') or kwargs to
            pass to :func:`mayavi.mlab.view()`.

        Returns
        -------
        view : tuple
            tuple returned from mlab.view
        roll : float
            camera roll
        distance : float | 'auto' | None
            distance from the origin
        row : int
            Row index of which brain to use
        col : int
            Column index of which brain to use
        """
        return self.brain_matrix[row][col].show_view(view, roll, distance)

    def set_distance(self, distance=None):
        """Set view distances for all brain plots to the same value

        Parameters
        ----------
        distance : float | None
            Distance to use. If None, brains are set to the farthest
            "best fit" distance across all current views; note that
            the underlying "best fit" function can be buggy.

        Returns
        -------
        distance : float
            The distance used.
        """
        if distance is None:
            distance = []
            for ff in self._figures:
                for f in ff:
                    mlab.view(figure=f, distance='auto')
                    v = mlab.view(figure=f)
                    # This should only happen for the test backend
                    if v is None:
                        v = [0, 0, 100]
                    distance += [v[2]]
            distance = max(distance)

        for ff in self._figures:
            for f in ff:
                mlab.view(distance=distance, figure=f)
        return distance

    def set_surf(self, surf):
        """Change the surface geometry

        Parameters
        ----------
        surf : str
            freesurfer surface mesh name (ie 'white', 'inflated', etc.)
        """
        if self.surf == surf:
            return

        views = self._toggle_render(False)

        # load new geometry
        for geo in self.geo.values():
            try:
                geo.surf = surf
                geo.load_geometry()
            except IOError:  # surface file does not exist
                geo.surf = self.surf
                self._toggle_render(True)
                raise

        # update mesh objects (they use a reference to geo.coords)
        for brain in self.brains:
            brain.update_surf()

        self.surf = surf
        self._toggle_render(True, views)

        for brain in self.brains:
            if brain._f.scene is not None:
                brain._f.scene.reset_zoom()

    @verbose
    def scale_data_colormap(self, fmin, fmid, fmax, transparent, verbose=None):
        """Scale the data colormap.

        Parameters
        ----------
        fmin : float
            minimum value of colormap
        fmid : float
            value corresponding to color midpoint
        fmax : float
            maximum value for colormap
        transparent : boolean
            if True: use a linear transparency between fmin and fmid
        verbose : bool, str, int, or None
            If not None, override default verbose level (see surfer.verbose).
        """
        if not (fmin < fmid) and (fmid < fmax):
            raise ValueError("Invalid colormap, we need fmin<fmid<fmax")

        # Cast inputs to float to prevent integer division
        fmin = float(fmin)
        fmid = float(fmid)
        fmax = float(fmax)

        logger.info("colormap: fmin=%0.2e fmid=%0.2e fmax=%0.2e "
                    "transparent=%d" % (fmin, fmid, fmax, transparent))

        # Get the original colormap
        for h in ['lh', 'rh']:
            data = self.data_dict[h]
            if data is not None:
                table = data["orig_ctable"].copy()

        # Add transparency if needed
        if transparent:
            n_colors = table.shape[0]
            n_colors2 = int(n_colors / 2)
            table[:n_colors2, -1] = np.linspace(0, 255, n_colors2)
            table[n_colors2:, -1] = 255 * np.ones(n_colors - n_colors2)

        # Scale the colormap
        table_new = table.copy()
        n_colors = table.shape[0]
        n_colors2 = int(n_colors / 2)

        # Index of fmid in new colorbar
        fmid_idx = int(np.round(n_colors * ((fmid - fmin) /
                                            (fmax - fmin))) - 1)

        # Go through channels
        for i in range(4):
            part1 = np.interp(np.linspace(0, n_colors2 - 1, fmid_idx + 1),
                              np.arange(n_colors),
                              table[:, i])
            table_new[:fmid_idx + 1, i] = part1
            part2 = np.interp(np.linspace(n_colors2, n_colors - 1,
                                          n_colors - fmid_idx - 1),
                              np.arange(n_colors),
                              table[:, i])
            table_new[fmid_idx + 1:, i] = part2

        views = self._toggle_render(False)
        # Use the new colormap
        for hemi in ['lh', 'rh']:
            data = self.data_dict[hemi]
            if data is not None:
                for surf in data['surfaces']:
                    cmap = surf.module_manager.scalar_lut_manager
                    cmap.load_lut_from_list(table_new / 255.)
                    cmap.data_range = np.array([fmin, fmax])

                # Update the data properties
                data["fmin"], data['fmid'], data['fmax'] = fmin, fmid, fmax
                data["transparent"] = transparent
        self._toggle_render(True, views)

    def set_data_time_index(self, time_idx, interpolation='quadratic'):
        """Set the data time index to show

        Parameters
        ----------
        time_idx : int | float
            Time index. Non-integer values will be displayed using
            interpolation between samples.
        interpolation : str
            Interpolation method (``scipy.interpolate.interp1d`` parameter,
            one of 'linear' | 'nearest' | 'zero' | 'slinear' | 'quadratic' |
            'cubic', default 'quadratic'). Interpolation is only used for
            non-integer indexes.
        """
        from scipy.interpolate import interp1d
        if self.n_times is None:
            raise RuntimeError('cannot set time index with no time data')
        if time_idx < 0 or time_idx >= self.n_times:
            raise ValueError("time index out of range")

        views = self._toggle_render(False)
        for hemi in ['lh', 'rh']:
            for data in self._data_dicts[hemi]:
                if data['array'].ndim == 1:
                    continue  # skip data without time axis

                # interpolation
                if isinstance(time_idx, float):
                    times = np.arange(self.n_times)
                    ifunc = interp1d(times, data['array'], interpolation, 1)
                    plot_data = ifunc(time_idx)
                else:
                    plot_data = data["array"][:, time_idx]

                if data["smooth_mat"] is not None:
                    plot_data = data["smooth_mat"] * plot_data

                for brain in self.brains:
                    if brain.hemi == hemi:
                        brain.set_data(data['layer_id'], plot_data)
                data["time_idx"] = time_idx

                # Update time label
                if data["time_label"]:
                    if isinstance(time_idx, float):
                        ifunc = interp1d(times, data['time'])
                        time = ifunc(time_idx)
                    else:
                        time = data["time"][time_idx]
                    self.update_text(data["time_label"](time), "time_label")
        self._toggle_render(True, views)

    @property
    def data_time_index(self):
        """Retrieve the currently displayed data time index

        Returns
        -------
        time_idx : int
            Current time index.

        Notes
        -----
        Raises a RuntimeError if the Brain instance has not data overlay.
        """
        for hemi in ['lh', 'rh']:
            data = self.data_dict[hemi]
            if data is not None:
                time_idx = data["time_idx"]
                return time_idx
        raise RuntimeError("Brain instance has no data overlay")

    @verbose
    def set_data_smoothing_steps(self, smoothing_steps, verbose=None):
        """Set the number of smoothing steps

        Parameters
        ----------
        smoothing_steps : int
            Number of smoothing steps
        verbose : bool, str, int, or None
            If not None, override default verbose level (see surfer.verbose).
        """
        views = self._toggle_render(False)
        for hemi in ['lh', 'rh']:
            data = self.data_dict[hemi]
            if data is not None:
                adj_mat = utils.mesh_edges(self.geo[hemi].faces)
                smooth_mat = utils.smoothing_matrix(data["vertices"],
                                                    adj_mat, smoothing_steps)
                data["smooth_mat"] = smooth_mat

                # Redraw
                if data["array"].ndim == 1:
                    plot_data = data["array"]
                else:
                    plot_data = data["array"][:, data["time_idx"]]

                plot_data = data["smooth_mat"] * plot_data
                for surf in data["surfaces"]:
                    surf.mlab_source.scalars = plot_data

                # Update data properties
                data["smoothing_steps"] = smoothing_steps
        self._toggle_render(True, views)

    def index_for_time(self, time, rounding='closest'):
        """Find the data time index closest to a specific time point.

        Parameters
        ----------
        time : scalar
            Time.
        rounding : 'closest' | 'up' | 'down'
            How to round if the exact time point is not an index.

        Returns
        -------
        index : int
            Data time index closest to time.
        """
        if self.n_times is None:
            raise RuntimeError("Brain has no time axis")
        times = self._times

        # Check that time is in range
        tmin = np.min(times)
        tmax = np.max(times)
        max_diff = (tmax - tmin) / (len(times) - 1) / 2
        if time < tmin - max_diff or time > tmax + max_diff:
            err = ("time = %s lies outside of the time axis "
                   "[%s, %s]" % (time, tmin, tmax))
            raise ValueError(err)

        if rounding == 'closest':
            idx = np.argmin(np.abs(times - time))
        elif rounding == 'up':
            idx = np.nonzero(times >= time)[0][0]
        elif rounding == 'down':
            idx = np.nonzero(times <= time)[0][-1]
        else:
            err = "Invalid rounding parameter: %s" % repr(rounding)
            raise ValueError(err)

        return idx

    def set_time(self, time):
        """Set the data time index to the time point closest to time

        Parameters
        ----------
        time : scalar
            Time.
        """
        idx = self.index_for_time(time)
        self.set_data_time_index(idx)

    def _get_colorbars(self, row, col):
        shape = self.brain_matrix.shape
        row = row % shape[0]
        col = col % shape[1]
        ind = np.ravel_multi_index((row, col), self.brain_matrix.shape)
        colorbars = []
        h = self._brain_list[ind]['hemi']
        if self.data_dict[h] is not None and 'colorbars' in self.data_dict[h]:
            colorbars.append(self.data_dict[h]['colorbars'][row])
        if len(self.morphometry_list) > 0:
            colorbars.append(self.morphometry_list[ind]['colorbar'])
        if len(self.contour_list) > 0:
            colorbars.append(self.contour_list[ind]['colorbar'])
        if len(self.overlays_dict) > 0:
            for name, obj in self.overlays_dict.items():
                for bar in ["pos_bar", "neg_bar"]:
                    try:  # deal with positive overlays
                        this_ind = min(len(obj) - 1, ind)
                        colorbars.append(getattr(obj[this_ind], bar))
                    except AttributeError:
                        pass
        return colorbars

    def _colorbar_visibility(self, visible, row, col):
        for cb in self._get_colorbars(row, col):
            if cb is not None:
                cb.visible = visible

    def show_colorbar(self, row=-1, col=-1):
        """Show colorbar(s) for given plot

        Parameters
        ----------
        row : int
            Row index of which brain to use
        col : int
            Column index of which brain to use
        """
        self._colorbar_visibility(True, row, col)

    def hide_colorbar(self, row=-1, col=-1):
        """Hide colorbar(s) for given plot

        Parameters
        ----------
        row : int
            Row index of which brain to use
        col : int
            Column index of which brain to use
        """
        self._colorbar_visibility(False, row, col)

    def close(self):
        """Close all figures and cleanup data structure."""
        for ri, ff in enumerate(self._figures):
            for ci, f in enumerate(ff):
                if f is not None:
                    mlab.close(f)
                    self._figures[ri][ci] = None

        # should we tear down other variables?
        if self._v is not None:
            self._v.dispose()
            self._v = None

    def __del__(self):
        if hasattr(self, '_v') and self._v is not None:
            self._v.dispose()
            self._v = None

    ###########################################################################
    # SAVING OUTPUT
    def save_single_image(self, filename, row=-1, col=-1):
        """Save view from one panel to disk

        Only mayavi image types are supported:
        (png jpg bmp tiff ps eps pdf rib  oogl iv  vrml obj

        Parameters
        ----------
        filename: string
            path to new image file
        row : int
            row index of the brain to use
        col : int
            column index of the brain to use

        Due to limitations in TraitsUI, if multiple views or hemi='split'
        is used, there is no guarantee painting of the windows will
        complete before control is returned to the command line. Thus
        we strongly recommend using only one figure window (which uses
        a Mayavi figure to plot instead of TraitsUI) if you intend to
        script plotting commands.
        """
        brain = self.brain_matrix[row, col]
        ftype = filename[filename.rfind('.') + 1:]
        good_ftypes = ['png', 'jpg', 'bmp', 'tiff', 'ps',
                       'eps', 'pdf', 'rib', 'oogl', 'iv', 'vrml', 'obj']
        if ftype not in good_ftypes:
            raise ValueError("Supported image types are %s"
                             % " ".join(good_ftypes))
        mlab.draw(brain._f)
        if mlab.options.backend != 'test':
            mlab.savefig(filename, figure=brain._f)

    def save_image(self, filename, mode='rgb', antialiased=False):
        """Save view from all panels to disk

        Only mayavi image types are supported:
        (png jpg bmp tiff ps eps pdf rib  oogl iv  vrml obj

        Parameters
        ----------
        filename: string
            path to new image file
        mode : string
            Either 'rgb' (default) to render solid background, or 'rgba' to
            include alpha channel for transparent background.
        antialiased : bool
            Antialias the image (see :func:`mayavi.mlab.screenshot`
            for details; see default False).

        Notes
        -----
        Due to limitations in TraitsUI, if multiple views or hemi='split'
        is used, there is no guarantee painting of the windows will
        complete before control is returned to the command line. Thus
        we strongly recommend using only one figure window (which uses
        a Mayavi figure to plot instead of TraitsUI) if you intend to
        script plotting commands.
        """
        from scipy import misc
        misc.imsave(filename, self.screenshot(mode, antialiased))

    def screenshot(self, mode='rgb', antialiased=False):
        """Generate a screenshot of current view.

        Wraps to :func:`mayavi.mlab.screenshot` for ease of use.

        Parameters
        ----------
        mode : string
            Either 'rgb' or 'rgba' for values to return.
        antialiased : bool
            Antialias the image (see :func:`mayavi.mlab.screenshot`
            for details; default False).

        Returns
        -------
        screenshot : array
            Image pixel values.

        Notes
        -----
        Due to limitations in TraitsUI, if multiple views or ``hemi='split'``
        is used, there is no guarantee painting of the windows will
        complete before control is returned to the command line. Thus
        we strongly recommend using only one figure window (which uses
        a Mayavi figure to plot instead of TraitsUI) if you intend to
        script plotting commands.
        """
        row = []
        for ri in range(self.brain_matrix.shape[0]):
            col = []
            n_col = 2 if self._hemi == 'split' else 1
            for ci in range(n_col):
                col += [self.screenshot_single(mode, antialiased,
                                               ri, ci)]
            row += [np.concatenate(col, axis=1)]
        data = np.concatenate(row, axis=0)
        return data

    def screenshot_single(self, mode='rgb', antialiased=False, row=-1, col=-1):
        """Generate a screenshot of current view from a single panel.

        Wraps to :func:`mayavi.mlab.screenshot` for ease of use.

        Parameters
        ----------
        mode: string
            Either 'rgb' or 'rgba' for values to return
        antialiased: bool
            Antialias the image (see :func:`mayavi.mlab.screenshot`
            for details).
        row : int
            row index of the brain to use
        col : int
            column index of the brain to use

        Returns
        -------
        screenshot: array
            Image pixel values

        Notes
        -----
        Due to limitations in TraitsUI, if multiple views or hemi='split'
        is used, there is no guarantee painting of the windows will
        complete before control is returned to the command line. Thus
        we strongly recommend using only one figure window (which uses
        a Mayavi figure to plot instead of TraitsUI) if you intend to
        script plotting commands.
        """
        brain = self.brain_matrix[row, col]
        if mlab.options.backend != 'test':
            return mlab.screenshot(brain._f, mode, antialiased)
        else:
            out = np.ones(tuple(self._scene_size) + (3,), np.uint8)
            out[0, 0, 0] = 0
            return out

    def save_imageset(self, prefix, views, filetype='png', colorbar='auto',
                      row=-1, col=-1):
        """Convenience wrapper for save_image

        Files created are prefix+'_$view'+filetype

        Parameters
        ----------
        prefix: string | None
            filename prefix for image to be created. If None, a list of
            arrays representing images is returned (not saved to disk).
        views: list
            desired views for images
        filetype: string
            image type
        colorbar: 'auto' | int | list of int | None
            For 'auto', the colorbar is shown in the middle view (default).
            For int or list of int, the colorbar is shown in the specified
            views. For ``None``, no colorbar is shown.
        row : int
            row index of the brain to use
        col : int
            column index of the brain to use

        Returns
        -------
        images_written: list
            all filenames written
        """
        if isinstance(views, string_types):
            raise ValueError("Views must be a non-string sequence"
                             "Use show_view & save_image for a single view")
        if colorbar == 'auto':
            colorbar = [len(views) // 2]
        elif isinstance(colorbar, int):
            colorbar = [colorbar]
        images_written = []
        for iview, view in enumerate(views):
            try:
                if colorbar is not None and iview in colorbar:
                    self.show_colorbar(row, col)
                else:
                    self.hide_colorbar(row, col)
                self.show_view(view, row=row, col=col)
                if prefix is not None:
                    fname = "%s_%s.%s" % (prefix, view, filetype)
                    images_written.append(fname)
                    self.save_single_image(fname, row, col)
                else:
                    images_written.append(self.screenshot_single(row=row,
                                                                 col=col))
            except ValueError:
                print("Skipping %s: not in view dict" % view)
        return images_written

    def save_image_sequence(self, time_idx, fname_pattern, use_abs_idx=True,
                            row=-1, col=-1, montage='single', border_size=15,
                            colorbar='auto', interpolation='quadratic'):
        """Save a temporal image sequence

        The files saved are named ``fname_pattern % pos`` where ``pos`` is a
        relative or absolute index (controlled by ``use_abs_idx``).

        Parameters
        ----------
        time_idx : array_like
            Time indices to save. Non-integer values will be displayed using
            interpolation between samples.
        fname_pattern : str
            Filename pattern, e.g. 'movie-frame_%0.4d.png'.
        use_abs_idx : bool
            If True the indices given by ``time_idx`` are used in the filename
            if False the index in the filename starts at zero and is
            incremented by one for each image (Default: True).
        row : int
            Row index of the brain to use.
        col : int
            Column index of the brain to use.
        montage : 'current' | 'single' | list
            Views to include in the images: 'current' uses the currently
            displayed image; 'single' (default) uses a single view, specified
            by the ``row`` and ``col`` parameters; a 1 or 2 dimensional list
            can be used to specify a complete montage. Examples:
            ``['lat', 'med']`` lateral and ventral views ordered horizontally;
            ``[['fro'], ['ven']]`` frontal and ventral views ordered
            vertically.
        border_size : int
            Size of image border (more or less space between images).
        colorbar : 'auto' | int | list of int | None
            For 'auto', the colorbar is shown in the middle view (default).
            For int or list of int, the colorbar is shown in the specified
            views. For ``None``, no colorbar is shown.
        interpolation : str
            Interpolation method (``scipy.interpolate.interp1d`` parameter,
            one of 'linear' | 'nearest' | 'zero' | 'slinear' | 'quadratic' |
            'cubic', default 'quadratic'). Interpolation is only used for
            non-integer indexes.

        Returns
        -------
        images_written : list
            All filenames written.
        """
        images_written = list()
        for i, idx in enumerate(self._iter_time(time_idx, interpolation)):
            fname = fname_pattern % (idx if use_abs_idx else i)
            if montage == 'single':
                self.save_single_image(fname, row, col)
            elif montage == 'current':
                self.save_image(fname)
            else:
                self.save_montage(fname, montage, 'h', border_size, colorbar,
                                  row, col)
            images_written.append(fname)

        return images_written

    def save_montage(self, filename, order=['lat', 'ven', 'med'],
                     orientation='h', border_size=15, colorbar='auto',
                     row=-1, col=-1):
        """Create a montage from a given order of images

        Parameters
        ----------
        filename: string | None
            path to final image. If None, the image will not be saved.
        order: list
            list of views: order of views to build montage (default
            ``['lat', 'ven', 'med']``; nested list of views to specify
            views in a 2-dimensional grid (e.g,
            ``[['lat', 'ven'], ['med', 'fro']]``)
        orientation: {'h' | 'v'}
            montage image orientation (horizontal of vertical alignment; only
            applies if ``order`` is a flat list)
        border_size: int
            Size of image border (more or less space between images)
        colorbar: 'auto' | int | list of int | None
            For 'auto', the colorbar is shown in the middle view (default).
            For int or list of int, the colorbar is shown in the specified
            views. For ``None``, no colorbar is shown.
        row : int
            row index of the brain to use
        col : int
            column index of the brain to use

        Returns
        -------
        out : array
            The montage image, usable with :func:`matplotlib.pyplot.imshow`.
        """
        # find flat list of views and nested list of view indexes
        assert orientation in ['h', 'v']
        if isinstance(order, (str, dict)):
            views = [order]
        elif all(isinstance(x, (str, dict)) for x in order):
            views = order
        else:
            views = []
            orientation = []
            for row_order in order:
                if isinstance(row_order, (str, dict)):
                    orientation.append([len(views)])
                    views.append(row_order)
                else:
                    orientation.append([])
                    for view in row_order:
                        orientation[-1].append(len(views))
                        views.append(view)

        if colorbar == 'auto':
            colorbar = [len(views) // 2]
        elif isinstance(colorbar, int):
            colorbar = [colorbar]
        brain = self.brain_matrix[row, col]

        # store current view + colorbar visibility
        with warnings.catch_warnings(record=True):  # traits focalpoint
            current_view = mlab.view(figure=brain._f)
        colorbars = self._get_colorbars(row, col)
        colorbars_visibility = dict()
        for cb in colorbars:
            if cb is not None:
                colorbars_visibility[cb] = cb.visible

        images = self.save_imageset(None, views, colorbar=colorbar, row=row,
                                    col=col)
        out = make_montage(filename, images, orientation, colorbar,
                           border_size)

        # get back original view and colorbars
        if current_view is not None:  # can be None with test backend
            with warnings.catch_warnings(record=True):  # traits focalpoint
                mlab.view(*current_view, figure=brain._f)
        for cb in colorbars:
            if cb is not None:
                cb.visible = colorbars_visibility[cb]
        return out

    def save_movie(self, fname, time_dilation=4., tmin=None, tmax=None,
                   framerate=24, interpolation='quadratic', codec=None,
                   bitrate=None, **kwargs):
        """Save a movie (for data with a time axis)

        The movie is created through the :mod:`imageio` module. The format is
        determined by the extension, and additional options can be specified
        through keyword arguments that depend on the format. For available
        formats and corresponding parameters see the imageio documentation:
        http://imageio.readthedocs.io/en/latest/formats.html#multiple-images

        .. Warning::
            This method assumes that time is specified in seconds when adding
            data. If time is specified in milliseconds this will result in
            movies 1000 times longer than expected.

        Parameters
        ----------
        fname : str
            Path at which to save the movie. The extension determines the
            format (e.g., `'*.mov'`, `'*.gif'`, ...; see the :mod:`imageio`
            documenttion for available formats).
        time_dilation : float
            Factor by which to stretch time (default 4). For example, an epoch
            from -100 to 600 ms lasts 700 ms. With ``time_dilation=4`` this
            would result in a 2.8 s long movie.
        tmin : float
            First time point to include (default: all data).
        tmax : float
            Last time point to include (default: all data).
        framerate : float
            Framerate of the movie (frames per second, default 24).
        interpolation : str
            Interpolation method (``scipy.interpolate.interp1d`` parameter,
            one of 'linear' | 'nearest' | 'zero' | 'slinear' | 'quadratic' |
            'cubic', default 'quadratic').
        **kwargs :
            Specify additional options for :mod:`imageio`.

        Notes
        -----
        Requires imageio package, which can be installed together with
        PySurfer with::

            $ pip install -U pysurfer[save_movie]
        """
        try:
            import imageio
        except ImportError:
            raise ImportError("Saving movies from PySurfer requires the "
                              "imageio library. To install imageio with pip, "
                              "run\n\n    $ pip install imageio\n\nTo "
                              "install/update PySurfer and imageio together, "
                              "run\n\n    $ pip install -U "
                              "pysurfer[save_movie]\n")
        from scipy.interpolate import interp1d

        # find imageio FFMPEG parameters
        if 'fps' not in kwargs:
            kwargs['fps'] = framerate
        if codec is not None:
            kwargs['codec'] = codec
        if bitrate is not None:
            kwargs['bitrate'] = bitrate

        # find tmin
        if tmin is None:
            tmin = self._times[0]
        elif tmin < self._times[0]:
            raise ValueError("tmin=%r is smaller than the first time point "
                             "(%r)" % (tmin, self._times[0]))

        # find indexes at which to create frames
        if tmax is None:
            tmax = self._times[-1]
        elif tmax > self._times[-1]:
            raise ValueError("tmax=%r is greater than the latest time point "
                             "(%r)" % (tmax, self._times[-1]))
        n_frames = floor((tmax - tmin) * time_dilation * framerate)
        times = np.arange(n_frames)
        times /= framerate * time_dilation
        times += tmin
        interp_func = interp1d(self._times, np.arange(self.n_times))
        time_idx = interp_func(times)

        n_times = len(time_idx)
        if n_times == 0:
            raise ValueError("No time points selected")

        logger.debug("Save movie for time points/samples\n%s\n%s"
                     % (times, time_idx))
        # Sometimes the first screenshot is rendered with a different
        # resolution on OS X
        self.screenshot()
        images = [self.screenshot() for _ in
                  self._iter_time(time_idx, interpolation)]
        imageio.mimwrite(fname, images, **kwargs)

    def animate(self, views, n_steps=180., fname=None, use_cache=False,
                row=-1, col=-1):
        """Animate a rotation.

        Currently only rotations through the axial plane are allowed.

        Parameters
        ----------
        views: sequence
            views to animate through
        n_steps: float
            number of steps to take in between
        fname: string
            If not None, it saves the animation as a movie.
            fname should end in '.avi' as only the AVI format is supported
        use_cache: bool
            Use previously generated images in ``./.tmp/``
        row : int
            Row index of the brain to use
        col : int
            Column index of the brain to use
        """
        brain = self.brain_matrix[row, col]
        gviews = list(map(brain._xfm_view, views))
        allowed = ('lateral', 'caudal', 'medial', 'rostral')
        if not len([v for v in gviews if v in allowed]) == len(gviews):
            raise ValueError('Animate through %s views.' % ' '.join(allowed))
        if fname is not None:
            if not fname.endswith('.avi'):
                raise ValueError('Can only output to AVI currently.')
            tmp_dir = './.tmp'
            tmp_fname = pjoin(tmp_dir, '%05d.png')
            if not os.path.isdir(tmp_dir):
                os.mkdir(tmp_dir)
        for i, beg in enumerate(gviews):
            try:
                end = gviews[i + 1]
                dv, dr = brain._min_diff(beg, end)
                dv /= np.array((n_steps))
                dr /= np.array((n_steps))
                brain.show_view(beg)
                for i in range(int(n_steps)):
                    brain._f.scene.camera.orthogonalize_view_up()
                    brain._f.scene.camera.azimuth(dv[0])
                    brain._f.scene.camera.elevation(dv[1])
                    brain._f.scene.renderer.reset_camera_clipping_range()
                    _force_render([[brain._f]], self._window_backend)
                    if fname is not None:
                        if not (os.path.isfile(tmp_fname % i) and use_cache):
                            self.save_single_image(tmp_fname % i, row, col)
            except IndexError:
                pass
        if fname is not None:
            fps = 10
            # we'll probably want some config options here
            enc_cmd = " ".join(["mencoder",
                                "-ovc lavc",
                                "-mf fps=%d" % fps,
                                "mf://%s" % tmp_fname,
                                "-of avi",
                                "-lavcopts vcodec=mjpeg",
                                "-ofps %d" % fps,
                                "-noskip",
                                "-o %s" % fname])
            ret = os.system(enc_cmd)
            if ret:
                print("\n\nError occured when exporting movie\n\n")


class _Hemisphere(object):
    """Object for visualizing one hemisphere with mlab"""
    def __init__(self, subject_id, hemi, figure, geo, geo_curv,
                 geo_kwargs, geo_reverse, subjects_dir, bg_color, backend):
        if hemi not in ['lh', 'rh']:
            raise ValueError('hemi must be either "lh" or "rh"')
        # Set the identifying info
        self.subject_id = subject_id
        self.hemi = hemi
        self.subjects_dir = subjects_dir
        self.viewdict = viewdicts[hemi]
        self._f = figure
        self._bg_color = bg_color
        self._backend = backend
        self.data = {}
        self._mesh_clones = {}  # surface mesh data-sources

        # mlab pipeline mesh and surface for geomtery
        meshargs = dict(scalars=geo.bin_curv) if geo_curv else dict()
        with warnings.catch_warnings(record=True):  # traits
            self._geo_mesh = mlab.pipeline.triangular_mesh_source(
                geo.x, geo.y, geo.z, geo.faces, figure=self._f, **meshargs)
        self._geo_mesh.data.points = geo.coords
        self._mesh_dataset = self._geo_mesh.mlab_source.dataset
        # add surface normals
        self._geo_mesh.data.point_data.normals = geo.nn
        self._geo_mesh.data.cell_data.normals = None
        if 'lut' in geo_kwargs:
            # create a new copy we can modify:
            geo_kwargs = dict(geo_kwargs)
            lut = geo_kwargs.pop('lut')
        else:
            lut = None
        with warnings.catch_warnings(record=True):  # traits warnings
            self._geo_surf = mlab.pipeline.surface(
               self._geo_mesh, figure=self._f, reset_zoom=True, **geo_kwargs)
        if lut is not None:
            lut_manager = self._geo_surf.module_manager.scalar_lut_manager
            lut_manager.load_lut_from_list(lut / 255.)
        if geo_curv and geo_reverse:
            curv_bar = mlab.scalarbar(self._geo_surf)
            curv_bar.reverse_lut = True
            curv_bar.visible = False

    def show_view(self, view=None, roll=None, distance=None):
        """Orient camera to display view"""
        if isinstance(view, string_types):
            try:
                vd = self._xfm_view(view, 'd')
                view = dict(azimuth=vd['v'][0], elevation=vd['v'][1])
                roll = vd['r']
            except ValueError as v:
                print(v)
                raise

        _force_render(self._f, self._backend)
        if view is not None:
            view['reset_roll'] = True
            view['figure'] = self._f
            view['distance'] = distance
            # DO NOT set focal point, can screw up non-centered brains
            # view['focalpoint'] = (0.0, 0.0, 0.0)
            mlab.view(**view)
        if roll is not None:
            mlab.roll(roll=roll, figure=self._f)
        _force_render(self._f, self._backend)

        view = mlab.view(figure=self._f)
        roll = mlab.roll(figure=self._f)

        return view, roll

    def _xfm_view(self, view, out='s'):
        """Normalize a given string to available view

        Parameters
        ----------
        view: string
            view which may match leading substring of available views

        Returns
        -------
        good: string
            matching view string
        out: {'s' | 'd'}
            's' to return string, 'd' to return dict

        """
        if view not in self.viewdict:
            good_view = [k for k in self.viewdict if view == k[:len(view)]]
            if len(good_view) == 0:
                raise ValueError('No views exist with this substring')
            if len(good_view) > 1:
                raise ValueError("Multiple views exist with this substring."
                                 "Try a longer substring")
            view = good_view[0]
        if out == 'd':
            return self.viewdict[view]
        else:
            return view

    def _min_diff(self, beg, end):
        """Determine minimum "camera distance" between two views.

        Parameters
        ----------
        beg : string
            origin anatomical view.
        end : string
            destination anatomical view.

        Returns
        -------
        diffs : tuple
            (min view "distance", min roll "distance").

        """
        beg = self._xfm_view(beg)
        end = self._xfm_view(end)
        if beg == end:
            dv = [360., 0.]
            dr = 0
        else:
            end_d = self._xfm_view(end, 'd')
            beg_d = self._xfm_view(beg, 'd')
            dv = []
            for b, e in zip(beg_d['v'], end_d['v']):
                diff = e - b
                # to minimize the rotation we need -180 <= diff <= 180
                if diff > 180:
                    dv.append(diff - 360)
                elif diff < -180:
                    dv.append(diff + 360)
                else:
                    dv.append(diff)
            dr = np.array(end_d['r']) - np.array(beg_d['r'])
        return (np.array(dv), dr)

    def _add_scalar_data(self, data):
        """Add scalar values to dataset"""
        if mlab.options.backend == 'test':
            # required SetActiveAttribute filter attributes are not set under
            # the testing backend
            return 0, self._geo_mesh
        array_id = self._mesh_dataset.point_data.add_array(data)
        self._mesh_dataset.point_data.get_array(array_id).name = array_id
        self._mesh_dataset.point_data.update()

        # build visualization pipeline
        with warnings.catch_warnings(record=True):
            pipe = mlab.pipeline.set_active_attribute(
                self._mesh_dataset, point_scalars=array_id, figure=self._f)
            # The new data-source is added to the wrong figure by default
            # (a Mayavi bug??)
            if pipe.parent not in self._f.children:
                self._f.add_child(pipe.parent)
        self._mesh_clones[array_id] = pipe.parent
        return array_id, pipe

    def _remove_scalar_data(self, array_id):
        """Removes scalar data"""
        if mlab.options.backend == 'test':
            # required SetActiveAttribute filter attributes are not set under
            # the testing backend
            return
        self._mesh_clones.pop(array_id).remove()
        self._mesh_dataset.point_data.remove_array(array_id)

    def add_overlay(self, old):
        """Add an overlay to the overlay dict from a file or array"""
        array_id, mesh = self._add_scalar_data(old.mlab_data)

        if old.pos_lims is not None:
            with warnings.catch_warnings(record=True):
                pos_thresh = threshold_filter(mesh, low=old.pos_lims[0])
                pos = mlab.pipeline.surface(
                    pos_thresh, colormap="YlOrRd", figure=self._f,
                    vmin=old.pos_lims[1], vmax=old.pos_lims[2])
                pos_bar = mlab.scalarbar(pos, nb_labels=5)
            pos_bar.reverse_lut = True
            pos_bar.scalar_bar_representation.position = (0.53, 0.01)
            pos_bar.scalar_bar_representation.position2 = (0.42, 0.09)
            self._format_cbar_text(pos_bar)
        else:
            pos = pos_bar = None

        if old.neg_lims is not None:
            with warnings.catch_warnings(record=True):
                neg_thresh = threshold_filter(mesh, up=old.neg_lims[0])
                neg = mlab.pipeline.surface(
                    neg_thresh, colormap="PuBu", figure=self._f,
                    vmin=old.neg_lims[1], vmax=old.neg_lims[2])
                neg_bar = mlab.scalarbar(neg, nb_labels=5)
            neg_bar.scalar_bar_representation.position = (0.05, 0.01)
            neg_bar.scalar_bar_representation.position2 = (0.42, 0.09)
            self._format_cbar_text(neg_bar)
        else:
            neg = neg_bar = None

        return OverlayDisplay(self, array_id, pos, pos_bar, neg, neg_bar)

    @verbose
    def add_data(self, array, mlab_plot, min, max, thresh, lut, colormap,
                 alpha, colorbar, layer_id):
        """Add data to the brain"""
        # Calculate initial data to plot
        if array.ndim == 1:
            array_plot = array
        elif array.ndim == 2:
            array_plot = array[:, 0]
        else:
            raise ValueError("data has to be 1D or 2D")

        array_id, pipe = self._add_scalar_data(mlab_plot)
        mesh = pipe.parent
        if thresh is not None:
            if array_plot.min() >= thresh:
                warn("Data min is greater than threshold.")
            else:
                with warnings.catch_warnings(record=True):
                    pipe = threshold_filter(pipe, low=thresh, figure=self._f)
        with warnings.catch_warnings(record=True):
            surf = mlab.pipeline.surface(
                pipe, colormap=colormap, vmin=min, vmax=max,
                opacity=float(alpha), figure=self._f)

        # apply look up table if given
        if lut is not None:
            lut_manager = surf.module_manager.scalar_lut_manager
            lut_manager.load_lut_from_list(lut / 255.)

        # Get the original colormap table
        orig_ctable = \
            surf.module_manager.scalar_lut_manager.lut.table.to_array().copy()

        # Get the colorbar
        if colorbar:
            bar = mlab.scalarbar(surf)
            self._format_cbar_text(bar)
            bar.scalar_bar_representation.position2 = .8, 0.09
        else:
            bar = None

        self.data[layer_id] = {'array_id': array_id, 'mesh': mesh}

        return surf, orig_ctable, bar

    def add_annotation(self, annot, ids, cmap):
        """Add an annotation file"""
        # Add scalar values to dataset
        array_id, pipe = self._add_scalar_data(ids)
        with warnings.catch_warnings(record=True):
            surf = mlab.pipeline.surface(pipe, name=annot, figure=self._f)

        # Set the color table
        lut_manager = surf.module_manager.scalar_lut_manager
        lut_manager.load_lut_from_list(cmap / 255.)

        # Set the brain attributes
        return dict(surface=surf, name=annot, colormap=cmap, brain=self,
                    array_id=array_id)

    def add_label(self, label, label_name, color, alpha):
        """Add an ROI label to the image"""
        from matplotlib.colors import colorConverter
        array_id, pipe = self._add_scalar_data(label)
        with warnings.catch_warnings(record=True):
            surf = mlab.pipeline.surface(pipe, name=label_name, figure=self._f)
        color = colorConverter.to_rgba(color, alpha)
        cmap = np.array([(0, 0, 0, 0,), color])
        lut_manager = surf.module_manager.scalar_lut_manager
        lut_manager.load_lut_from_list(cmap)
        return array_id, surf

    def add_morphometry(self, morph_data, colormap, measure,
                        min, max, colorbar):
        """Add a morphometry overlay to the image"""
        array_id, pipe = self._add_scalar_data(morph_data)
        with warnings.catch_warnings(record=True):
            surf = mlab.pipeline.surface(pipe, colormap=colormap,
                                         vmin=min, vmax=max,
                                         name=measure, figure=self._f)

        # Get the colorbar
        if colorbar:
            bar = mlab.scalarbar(surf)
            self._format_cbar_text(bar)
            bar.scalar_bar_representation.position2 = .8, 0.09
        else:
            bar = None

        # Fil in the morphometry dict
        return dict(surface=surf, colorbar=bar, measure=measure, brain=self,
                    array_id=array_id)

    def add_foci(self, foci_coords, scale_factor, color, alpha, name):
        """Add spherical foci, possibly mapping to displayed surf"""
        # Create the visualization
        with warnings.catch_warnings(record=True):  # traits
            points = mlab.points3d(
                foci_coords[:, 0], foci_coords[:, 1], foci_coords[:, 2],
                np.ones(foci_coords.shape[0]), name=name, figure=self._f,
                scale_factor=(10. * scale_factor), color=color, opacity=alpha)
        return points

    def add_contour_overlay(self, scalar_data, min=None, max=None,
                            n_contours=7, line_width=1.5, lut=None,
                            colorbar=True):
        """Add a topographic contour overlay of the positive data"""
        array_id, pipe = self._add_scalar_data(scalar_data)
        with warnings.catch_warnings(record=True):
            thresh = threshold_filter(pipe, low=min)
            surf = mlab.pipeline.contour_surface(thresh, contours=n_contours,
                                                 line_width=line_width)
        if lut is not None:
            lut_manager = surf.module_manager.scalar_lut_manager
            lut_manager.load_lut_from_list(lut / 255.)

        # Set the colorbar and range correctly
        with warnings.catch_warnings(record=True):  # traits
            bar = mlab.scalarbar(surf, nb_colors=n_contours,
                                 nb_labels=n_contours + 1)
        bar.data_range = min, max
        self._format_cbar_text(bar)
        bar.scalar_bar_representation.position2 = .8, 0.09
        if not colorbar:
            bar.visible = False

        # Set up a dict attribute with pointers at important things
        return dict(surface=surf, colorbar=bar, brain=self, array_id=array_id)

    def add_text(self, x, y, text, name, color=None, opacity=1.0):
        """ Add a text to the visualization"""
        with warnings.catch_warnings(record=True):
            text = mlab.text(x, y, text, name=name, color=color,
                             opacity=opacity, figure=self._f)
            return text

    def remove_data(self, layer_id):
        "Remove data shown with .add_data()"
        data = self.data.pop(layer_id)
        self._remove_scalar_data(data['array_id'])

    def set_data(self, layer_id, values):
        "Set displayed data"
        data = self.data[layer_id]
        self._mesh_dataset.point_data.get_array(
            data['array_id']).from_array(values)
        # avoid "AttributeError: 'Scene' object has no attribute 'update'"
        if mlab.options.backend != 'test':
            data['mesh'].update()

    def _orient_lights(self):
        """Set lights to come from same direction relative to brain."""
        if self.hemi == "rh":
            if self._f.scene is not None and \
                    self._f.scene.light_manager is not None:
                for light in self._f.scene.light_manager.lights:
                    light.azimuth *= -1

    def _format_cbar_text(self, cbar):
        bg_color = self._bg_color
        if bg_color is None or sum(bg_color) < 2:
            text_color = (1., 1., 1.)
        else:
            text_color = (0., 0., 0.)
        cbar.label_text_property.color = text_color

    def update_surf(self):
        "Update surface mesh after mesh coordinates change"
        self._geo_mesh.update()
        for mesh in self._mesh_clones.values():
            mesh.update()


class OverlayData(object):
    """Encapsulation of statistical neuroimaging overlay viz data"""

    def __init__(self, scalar_data, min, max, sign):
        if scalar_data.min() >= 0:
            sign = "pos"
        elif scalar_data.max() <= 0:
            sign = "neg"

        if sign in ["abs", "pos"]:
            # Figure out the correct threshold to avoid TraitErrors
            # This seems like not the cleanest way to do this
            pos_max = np.max((0.0, np.max(scalar_data)))
            if pos_max < min:
                thresh_low = pos_max
            else:
                thresh_low = min
            self.pos_lims = [thresh_low, min, max]
        else:
            self.pos_lims = None

        if sign in ["abs", "neg"]:
            # Figure out the correct threshold to avoid TraitErrors
            # This seems even less clean due to negative convolutedness
            neg_min = np.min((0.0, np.min(scalar_data)))
            if neg_min > -min:
                thresh_up = neg_min
            else:
                thresh_up = -min
            self.neg_lims = [thresh_up, -max, -min]
        else:
            self.neg_lims = None
        # Byte swap copy; due to mayavi bug
        self.mlab_data = _prepare_data(scalar_data)


class OverlayDisplay():
    """Encapsulation of overlay viz plotting"""

    def __init__(self, brain, array_id, pos, pos_bar, neg, neg_bar):
        self._brain = brain
        self._array_id = array_id
        self.pos = pos
        self.pos_bar = pos_bar
        self.neg = neg
        self.neg_bar = neg_bar

    def remove(self):
        self._brain._remove_scalar_data(self._array_id)
        if self.pos_bar is not None:
            self.pos_bar.visible = False
        if self.neg_bar is not None:
            self.neg_bar.visible = False


class TimeViewer(HasTraits):
    """TimeViewer object providing a GUI for visualizing time series

    Useful for visualizing M/EEG inverse solutions on Brain object(s).

    Parameters
    ----------
    brain : Brain (or list of Brain)
        brain(s) to control
    """
    # Nested import of traisui for setup.py without X server
    from traitsui.api import (View, Item, VSplit, HSplit, Group)
    min_time = Int(0)
    max_time = Int(1E9)
    current_time = Range(low="min_time", high="max_time", value=0)
    # colormap: only update when user presses Enter
    fmax = Float(enter_set=True, auto_set=False)
    fmid = Float(enter_set=True, auto_set=False)
    fmin = Float(enter_set=True, auto_set=False)
    transparent = Bool(True)
    smoothing_steps = Int(20, enter_set=True, auto_set=False,
                          desc="number of smoothing steps. Use -1 for"
                               "automatic number of steps")
    orientation = Enum("lateral", "medial", "rostral", "caudal",
                       "dorsal", "ventral", "frontal", "parietal")

    # GUI layout
    view = View(VSplit(Item(name="current_time"),
                       Group(HSplit(Item(name="fmin"),
                                    Item(name="fmid"),
                                    Item(name="fmax"),
                                    Item(name="transparent")
                                    ),
                             label="Color scale",
                             show_border=True),
                       Item(name="smoothing_steps"),
                       Item(name="orientation")
                       )
                )

    def __init__(self, brain):
        super(TimeViewer, self).__init__()

        if isinstance(brain, (list, tuple)):
            self.brains = brain
        else:
            self.brains = [brain]

        # Initialize GUI with values from first brain
        props = self.brains[0].get_data_properties()

        self._disable_updates = True
        self.max_time = len(props["time"]) - 1
        self.current_time = props["time_idx"]
        self.fmin = props["fmin"]
        self.fmid = props["fmid"]
        self.fmax = props["fmax"]
        self.transparent = props["transparent"]
        if props["smoothing_steps"] is None:
            self.smoothing_steps = -1
        else:
            self.smoothing_steps = props["smoothing_steps"]
        self._disable_updates = False

        # Make sure all brains have the same time points
        for brain in self.brains[1:]:
            this_props = brain.get_data_properties()
            if not np.all(props["time"] == this_props["time"]):
                raise ValueError("all brains must have the same time"
                                 "points")

        # Show GUI
        self.configure_traits()

    @on_trait_change("smoothing_steps")
    def set_smoothing_steps(self):
        """ Change number of smooting steps
        """
        if self._disable_updates:
            return

        smoothing_steps = self.smoothing_steps
        if smoothing_steps < 0:
            smoothing_steps = None

        for brain in self.brains:
            brain.set_data_smoothing_steps(self.smoothing_steps)

    @on_trait_change("orientation")
    def set_orientation(self):
        """ Set the orientation
        """
        if self._disable_updates:
            return

        for brain in self.brains:
            brain.show_view(view=self.orientation)

    @on_trait_change("current_time")
    def set_time_point(self):
        """ Set the time point shown
        """
        if self._disable_updates:
            return

        for brain in self.brains:
            brain.set_data_time_index(self.current_time)

    @on_trait_change("fmin, fmid, fmax, transparent")
    def scale_colormap(self):
        """ Scale the colormap
        """
        if self._disable_updates:
            return

        for brain in self.brains:
            brain.scale_data_colormap(self.fmin, self.fmid, self.fmax,
                                      self.transparent)
