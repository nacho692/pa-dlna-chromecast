# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import contextlib
from pathlib import Path

__version__ = 'Unknown version'
def conf_py_setup():
    global __version__

    cur_path = Path(__file__).parent
    root_path = cur_path.parent.parent
    with contextlib.chdir(str(root_path)):
        from pa_dlna import __version__

    with open(cur_path / 'README.rst', 'w') as fdest:
        fdest.write('pa-dlna |version|\n')
        fdest.write('=================\n\n')
        with open(root_path / 'README.rst') as fsrc:
            content = fsrc.read()
            fdest.write(content)

conf_py_setup()

project = 'pa-dlna'
copyright = '2024, Xavier de Gaye'
author = ''

# The short X.Y version
version = __version__
# The full version, including alpha/beta/rc tags
release = __version__

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = ['sphinx.ext.imgconverter']
templates_path = ['_templates']
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

# -- Options for manual page output ------------------------------------------

# One entry per manual page. List of tuples
# (source start file, name, description, authors, manual section).
man_pages = [
    ('upnp-cmd', 'upnp-cmd', 'interactive command line tool for introspection'
     ' and control of UPnP devices',
     [author], 7),
    ('pa-dlna', 'pa-dlna', 'UPnP control point forwarding PulseAudio streams'
     ' to DLNA devices', [author], 7),
]
