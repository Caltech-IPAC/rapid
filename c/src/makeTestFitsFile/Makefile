include $(RAPID_SW)/c/builds/definitions.mk
BIN_NAME = makeTestFitsFile

COMPILE.c = $(CC) $(CFLAGS) $(CPPFLAGS) $(TARGET_ARCH) -c
LINK.c = $(CC) $(LINKFLAGS)
CC = g++
CFLAGS = -fPIC

CPPFLAGS = -I $(RAPID_INC_PATH) -I $(RAPID_INC_PATH)/cfitsio -I $(RAPID_INC_PATH)/nan
LINKFLAGS = -lm -lgsl -lgslcblas -lcfitsio -lnan -L$(RAPID_LIB_PATH)  

OBJECTS = makeTestFitsFile.o 

all: $(BIN_NAME)

$(BIN_NAME): $(OBJECTS)
	$(LINK.c) $(OUTPUT_OPTION) $(OBJECTS)
	if [ -f $(BIN_NAME) ]; then \
		$(MV) $(BIN_NAME) $(RAPID_BIN_PATH); \
	fi;

.PHONY: clean
clean:
	rm *.o
	if [ -f $(RAPID_BIN_PATH)/$(BIN_NAME) ]; then \
		$(RM) $(RAPID_BIN_PATH)/$(BIN_NAME); \
	fi;

%.o: %.c
	$(COMPILE.c) $<

