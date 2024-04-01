include $(RAPID_SW)/c/builds/definitions.mk
BIN_NAME = generateSmoothLampPattern
CPPFLAGS = -I $(RAPID_INC_PATH)/cfitsio -I $(RAPID_INC_PATH)/nan -I $(RAPID_INC_PATH)/numericalrecipes
LINKFLAGS = -lcfitsio -lm -lnan -lnumericalrecipes -lpthread -L$(RAPID_LIB_PATH)

OBJECTS = $(BIN_NAME).o

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


