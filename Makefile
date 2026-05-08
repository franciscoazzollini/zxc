# Makefile for OmniComp
#
# Usage:
#   make            - build the shared library
#   make test       - build and run the round-trip test
#   make clean      - remove build artifacts
#
# Optional environment variables:
#   ZSTD_INCLUDE  - path to a zstd.h directory   (default: system include path)
#   ZSTD_LIB      - path to a libzstd directory  (default: system library path)
#   CC            - C compiler                   (default: cc)

CC      ?= cc
CFLAGS  ?= -O3 -ffast-math -fPIC -Wall -Wextra
PTHREAD ?= -pthread

# Use -march=native by default for AVX2 auto-vectorisation.
# Disable by exporting OMNICOMP_NATIVE=0 (useful for cross-builds).
ifneq ($(OMNICOMP_NATIVE), 0)
  CFLAGS += -march=native
endif

ZSTD_INCLUDE ?=
ZSTD_LIB     ?=

ifneq ($(ZSTD_INCLUDE),)
  CFLAGS += -I$(ZSTD_INCLUDE)
endif
ifneq ($(ZSTD_LIB),)
  LDFLAGS += -L$(ZSTD_LIB) -Wl,-rpath,$(ZSTD_LIB)
endif

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  SHARED_EXT := dylib
  SHARED_FLAGS := -dynamiclib
else
  SHARED_EXT := so
  SHARED_FLAGS := -shared
endif

LIB_NAME := libomnicomp_pipeline.$(SHARED_EXT)
LIB_PATH := omnicomp/$(LIB_NAME)
SRC      := omnicomp/pipeline.c

.PHONY: all test clean

all: $(LIB_PATH)

$(LIB_PATH): $(SRC)
	$(CC) $(CFLAGS) $(SHARED_FLAGS) $(PTHREAD) $(SRC) $(LDFLAGS) -lzstd -lm -o $(LIB_PATH)

test: $(LIB_PATH)
	python3 -m pytest tests/ -v

clean:
	rm -f omnicomp/*.so omnicomp/*.dylib
