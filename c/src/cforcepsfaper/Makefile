###################################################################################
#     Makefile              2/4/2026          Russ Laher
#                                             laher@ipac.caltech.edu
#                                             California Institute of Technology
#                                             (c) 2026, All Rights Reserved.
#
#    The C-code cforcepsfaper is multi-threaded, intended to execute the
#    compute-intensive parts of forced photometry.
###################################################################################

include $(RAPID_SW)/c/builds/definitions.mk

BIN_NAME = cforcepsfaper
CPPFLAGS = -I $(RAPID_INC_PATH)/cfitsio -I $(RAPID_INC_PATH)/nan -I $(RAPID_INC_PATH)/numericalrecipes
LINKFLAGS = -lcfitsio -lm -lnan -lnumericalrecipes -lpthread -L$(RAPID_LIB_PATH)

OBJECTS = cforcepsfaper.o

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


