include $(RAPID_SW)/c/builds/definitions.mk
BIN_NAME = fitsverify 
CPPFLAGS = -I $(RAPID_INC_PATH)/cfitsio -DSTANDALONE
LINKFLAGS = -lm -lcfitsio -L$(RAPID_LIB_PATH)

OBJECTS = ftverify.o fvrf_data.o fvrf_file.o fvrf_head.o fvrf_key.o fvrf_misc.o


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


