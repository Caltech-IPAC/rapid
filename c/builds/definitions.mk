RAPID_BIN_PATH = $(RAPID_SW)/c/bin
RAPID_LIB_PATH = $(RAPID_SW)/c/lib
RAPID_INC_PATH = $(RAPID_SW)/c/include
COMPILE.c = $(CC) $(CFLAGS) $(CPPFLAGS) $(TARGET_ARCH) -c
LINK.c = $(CC) $(LINKFLAGS)
CC = gcc -O2
CFLAGS = -fPIC -std=c99
OUTPUT_OPTION = -o $@
AR = ar
ARFLAGS = rv
RANLIB = ranlib
RM = rm
CP = cp
MV = mv
ifdef DYLD_LIBRARY_PATH
   SHLIB_SUFFIX =  .dylib 
   SHLIB_LD = gcc -dynamiclib
   SHLIB_LD_ALT = gcc
else
   SHLIB_SUFFIX =  .so
   SHLIB_LD = gcc -shared
   SHLIB_LD_ALT = gcc -shared
endif
