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
#
# On macOS without Xcode zstd headers, Homebrew is typical:
#   brew install zstd
# This Makefile auto-adds -I/-L for common Homebrew prefixes when unset.

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

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  ifeq ($(ZSTD_INCLUDE),)
    ifneq ($(wildcard /opt/homebrew/opt/zstd/include/zstd.h),)
      ZSTD_INCLUDE := /opt/homebrew/opt/zstd/include
      ZSTD_LIB     := /opt/homebrew/opt/zstd/lib
    else ifneq ($(wildcard /usr/local/opt/zstd/include/zstd.h),)
      ZSTD_INCLUDE := /usr/local/opt/zstd/include
      ZSTD_LIB     := /usr/local/opt/zstd/lib
    endif
  endif
endif

ifneq ($(ZSTD_INCLUDE),)
  CFLAGS += -I$(ZSTD_INCLUDE)
endif
# ZSTD_STATIC=1 links libzstd.a from ZSTD_LIB (self-contained dylib/so; no
# runtime libzstd). Requires ZSTD_LIB pointing at a directory containing
# libzstd.a (e.g. a source build under lib/ after ``make lib-release``).

ZSTD_STATIC ?= 0
ifeq ($(ZSTD_STATIC),1)
  ifeq ($(ZSTD_LIB),)
    $(error ZSTD_STATIC=1 requires ZSTD_LIB to the directory containing libzstd.a)
  endif
  ZSTD_LIBS := $(ZSTD_LIB)/libzstd.a
else
  ZSTD_LIBS := -lzstd
endif

ifneq ($(ZSTD_LIB),)
  LDFLAGS += -L$(ZSTD_LIB) -Wl,-rpath,$(ZSTD_LIB)
endif

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
	$(CC) $(CFLAGS) $(SHARED_FLAGS) $(PTHREAD) $(SRC) $(LDFLAGS) $(ZSTD_LIBS) -lm -o $(LIB_PATH)

test: $(LIB_PATH)
	python3 -m pytest tests/ -v

clean:
	rm -f omnicomp/*.so omnicomp/*.dylib
