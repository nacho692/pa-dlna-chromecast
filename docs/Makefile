# Minimal makefile for Sphinx documentation
#

# You can set these variables from the command line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = source
BUILDDIR      = build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).
.ONESHELL:
%: Makefile
	@args=""
	for arg in "$@"
	do
	    case $$arg in
		clean | html | man)
		    if [ -z "$$args" ]; then
			args=$$arg
		    else
			args="$$args $$arg"
		    fi;;
		latexpdf)
		    latexpdf="latexpdf";;
		*) echo -n "Unknown target: $$arg";;
	    esac
	done

	if [ -n "$$args" ]; then
	    cp $(SOURCEDIR)/index.rst_html $(SOURCEDIR)/index.rst;
	    rm -f $(SOURCEDIR)/README.rst;
	    $(SPHINXBUILD) -M $$args "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
	fi
	if [ -n "$$latexpdf" ]; then
	    $$(cd $(SOURCEDIR) && ln -sf ../../README.rst README.rst);
	    cp $(SOURCEDIR)/index.rst_pdf $(SOURCEDIR)/index.rst;
	    $(SPHINXBUILD) -M latexpdf "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
	fi
