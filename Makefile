PYTHON ?= python3
.PHONY: test demo collector
test:
	$(PYTHON) -m unittest discover -s tests -v
demo:
	$(PYTHON) -m micropricelab demo
collector:
	$(MAKE) -C vendor/mdpipe build/book-core
