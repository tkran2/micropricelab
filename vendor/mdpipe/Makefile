CXX ?= c++
PYTHON ?= python3
CPPFLAGS += -Iinclude
CXXFLAGS ?= -O3 -DNDEBUG
CXXFLAGS += -std=c++20 -Wall -Wextra -Wpedantic -Wconversion -Wshadow
BUILD ?= build
# Honour an explicitly selected SDK without pinning another developer's Mac.
ifeq ($(shell uname -s),Darwin)
ifneq ($(SDKROOT),)
CPPFLAGS += -isysroot $(SDKROOT)
endif
endif

.PHONY: all test demo faults bench sanitize clean setup
all: $(BUILD)/book-core $(BUILD)/core-bench
$(BUILD):
	mkdir -p $(BUILD)
$(BUILD)/book-core: src/main.cpp include/mdp/book.hpp include/mdp/protocol.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@ $(LDFLAGS)
$(BUILD)/core-bench: src/benchmark.cpp include/mdp/book.hpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@ $(LDFLAGS)
test: all
	MDPIPE_CORE=$(CURDIR)/$(BUILD)/book-core $(PYTHON) -m unittest discover -s tests -v
demo: all
	$(PYTHON) -m mdpipe demo
faults: all
	$(PYTHON) -m mdpipe faults
bench: all
	$(BUILD)/core-bench --events 100000 --trials 5
sanitize:
	$(MAKE) BUILD=build-sanitize CXXFLAGS='-std=c++20 -O1 -g -Wall -Wextra -Wpedantic -Wconversion -Wshadow -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' test
setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
clean:
	rm -rf build build-sanitize
