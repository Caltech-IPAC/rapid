include $(RAPID_SW)/c/builds/definitions.mk
BIN_NAME = bkgest
CPPFLAGS = -I $(RAPID_INC_PATH)/cfitsio -I $(RAPID_INC_PATH)/nan -I $(RAPID_INC_PATH)/numericalrecipes
LINKFLAGS = -lcfitsio -lm -lnan -lnumericalrecipes -lpthread -L$(RAPID_LIB_PATH)

OBJECTS = bkgest_compute_results.o bkgest_exec.o bkgest_exit.o bkgest_expand_envvar.o bkgest_init_constants.o bkgest_log_writer.o bkgest_output.o bkgest_output_constkeywords.o bkgest_output_filenkeywords.o bkgest_output_keywords.o bkgest_output_stdkeywords.o bkgest_parse_args.o bkgest_parse_namelist.o bkgest_read_data.o bkgest_read_image1.o bkgest_read_mask.o

all: $(BIN_NAME)

$(BIN_NAME): $(OBJECTS)
	$(LINK.c) $(OUTPUT_OPTION) $(OBJECTS)
	if [ -f $(BIN_NAME) ]; then \
		$(MV) $(BIN_NAME) $(RAPID_BIN_PATH); \
		$(CP) $(BIN_NAME).h $(RAPID_INC_PATH); \
		$(CP) $(BIN_NAME)_errcodes.h $(RAPID_INC_PATH); \
	fi;

.PHONY: clean
clean:
	$(RM) *.o
	if [ -f $(RAPID_BIN_PATH)/$(BIN_NAME) ]; then \
		$(RM) $(RAPID_BIN_PATH)/$(BIN_NAME); \
	fi;
	if [ -f $(RAPID_INC_PATH)/$(BIN_NAME).h ]; then \
		$(RM) $(RAPID_INC_PATH)/$(BIN_NAME).h; \
		$(RM) $(RAPID_INC_PATH)/$(BIN_NAME)_errcodes.h; \
	fi;

%.o: %.c
	$(COMPILE.c) $<
