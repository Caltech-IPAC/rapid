include $(RAPID_SW)/c/builds/definitions.mk
BIN_NAME = imheaders
CPPFLAGS = -I $(RAPID_INC_PATH)/cfitsio
LINKFLAGS =  -lm -lcfitsio -L$(RAPID_LIB_PATH)

OBJECTS = imheaders.o


all: $(BIN_NAME)

$(BIN_NAME): $(OBJECTS)
	$(LINK.c) $(OUTPUT_OPTION) $(OBJECTS)
	if [ -f $(BIN_NAME) ]; then \
		$(MV) $(BIN_NAME) $(RAPID_BIN_PATH); \
	fi;

.PHONY: clean
clean:
	$(RM) *.o
	if [ -f $(RAPID_BIN_PATH)/$(BIN_NAME) ]; then \
		$(RM) $(RAPID_BIN_PATH)/$(BIN_NAME); \
	fi;

%.o: %.c
	$(COMPILE.c) $<


