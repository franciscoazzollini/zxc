# Makefile for OmniComp
#
# Usage:
#   make            - build the shared library
#   make test       - build and run the round-trip test
#   make clean      - remove build artifacts
#
# Optional environment variables:
#   USE_BUNDLED_ZSTD  auto (default) | 1 | 0
#                     auto: if third_party/zstd/lib/zstd.h exists, compile Meta's
#                     zstd from that tree, link libzstd.a into libomnicomp_pipeline
#                     (no runtime libzstd). Otherwise use system -lzstd.
#   ZSTD_INCLUDE  - path to zstd.h directory (default: bundled lib/ or system)
#   ZSTD_LIB      - path to libzstd directory (default: bundled or Homebrew)
#   ZSTD_STATIC   - 1 to link libzstd.a from ZSTD_LIB (ignored when bundled)
#   CC            - C compiler                   (default: cc)
#
# On macOS without the zstd submodule, Homebrew is typical:
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

USE_BUNDLED_ZSTD ?= auto
ifeq ($(USE_BUNDLED_ZSTD),auto)
  ifneq ($(wildcard third_party/zstd/lib/zstd.h),)
    USE_BUNDLED_ZSTD := 1
  else
    USE_BUNDLED_ZSTD := 0
  endif
endif

ZSTD_VENDOR_DIR := third_party/zstd/lib
ZSTD_VENDOR_A  := $(ZSTD_VENDOR_DIR)/libzstd.a

ZSTD_INCLUDE :=
ZSTD_LIB     :=

ifeq ($(USE_BUNDLED_ZSTD),1)
  ZSTD_INCLUDE := $(ZSTD_VENDOR_DIR)
  ZSTD_LIB     := $(ZSTD_VENDOR_DIR)
  ZSTD_STATIC  := 1
else
  ZSTD_STATIC ?= 0
endif

UNAME_S := $(shell uname -s)
ifeq ($(USE_BUNDLED_ZSTD),0)
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
endif

ifneq ($(ZSTD_INCLUDE),)
  CFLAGS += -I$(ZSTD_INCLUDE)
endif

# ZSTD_STATIC=1 links libzstd.a from ZSTD_LIB (self-contained dylib/so; no
# runtime libzstd). Bundled builds set this automatically.

ifeq ($(ZSTD_STATIC),1)
  ifeq ($(ZSTD_LIB),)
    $(error ZSTD_STATIC=1 requires ZSTD_LIB to the directory containing libzstd.a)
  endif
  ZSTD_LIBS := $(ZSTD_LIB)/libzstd.a
else
  ZSTD_LIBS := -lzstd
endif

ifneq ($(ZSTD_LIB),)
  ifeq ($(ZSTD_STATIC),1)
    LDFLAGS += -L$(ZSTD_LIB)
  else
    LDFLAGS += -L$(ZSTD_LIB) -Wl,-rpath,$(ZSTD_LIB)
  endif
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

ifeq ($(USE_BUNDLED_ZSTD),1)
  ZSTD_BUILD_PREREQ := $(ZSTD_VENDOR_A)
else
  ZSTD_BUILD_PREREQ :=
endif

.PHONY: all test clean

all: $(LIB_PATH)

$(ZSTD_VENDOR_A):
	$(MAKE) -C $(ZSTD_VENDOR_DIR) lib-release

$(LIB_PATH): $(SRC) $(ZSTD_BUILD_PREREQ)
	$(CC) $(CFLAGS) $(SHARED_FLAGS) $(PTHREAD) $(SRC) $(LDFLAGS) $(ZSTD_LIBS) -lm -o $(LIB_PATH)

test: $(LIB_PATH)
	python3 -m pytest tests/ -v

clean:
	rm -f omnicomp/*.so omnicomp/*.dylib
