# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys

sys.path.insert(0, os.path.abspath('../..'))
from pa_dlna import __version__, pa_dlna

project = 'pa-dlna'
copyright = '2022, Xavier de Gaye'
author = 'Xavier de Gaye'

# The short X.Y version
version = __version__
# The full version, including alpha/beta/rc tags
release = __version__

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = []
templates_path = ['_templates']
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'alabaster'
html_static_path = ['_static']
html_theme_options = {
    # Add an unicode NO-BREAK SPACE at the end of the string.
    'description': pa_dlna.__doc__ + '\n\n\n\u00A0',
    'fixed_sidebar': 'true',
    'page_width': '90%',
    'body_max_width': 'auto',
}
html_sidebars = {
    '**': [
        'about.html',
        'navigation.html',
        'relations.html',
        'searchbox.html',
        'donate.html',
    ]
}

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
