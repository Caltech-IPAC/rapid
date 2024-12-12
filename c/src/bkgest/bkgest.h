#include "fitsio.h"

#define BKEVERSN                      1.3
#define PROGRAM                       "bkgest"
#define PROGRAMABB                    "BKE"

#define MAX_FILENAME_LENGTH 1024
#define I_FCNNAME_LENGTH 256
#define I_MESSAGE_LENGTH 256
#define STRING_BUFFER_SIZE 2048

#define LEFT_MASK   0xffff0000
#define RIGHT_MASK  0x0000ffff

#define FNANMASK   0x7F80 /* mask bits 1 - 8; all set on NaNs */
#define DNANMASK   0x7FF0 /* mask bits 1 - 11; all set on NaNs */





