/***********************************************
bkgest_errcodes.h

Purpose

Define constant values.

Overview


Definitions of Variables

See *_init_constants and "main" function *_exec, where * is module name.
***********************************************/

/* Must list function codes first.  LOG_WRITER is special. */

#define LOG_WRITER         0x0000ffff
#define EXEC   0x00010000
#define INIT_CONSTANTS  0x00020000
#define INIT   0x00030000
#define MKBASIS   0x00040000
#define TRANSPMULT  0x00050000
#define DETCOMP   0x00060000
#define PSEUDOCOMP  0x00070000
#define LOG_LIKELYHOOD  0x00080000
#define VEC_MULT  0x00090000
#define INNER_PROD  0x000a0000
#define LL_COMP   0x000b0000
#define MAX_LLIKELYHOOD  0x000c0000
#define MAXLL_FINDMAX  0x000d0000
#define SSORTP   0x000e0000
#define CHARGE_ADDITION  0x000f0000
#define ENDPOINTS  0x00100000
#define ENDPOINT_FAR  0x00110000
#define ENDPOINT_NEAR  0x00120000
#define PROB_RADHIT  0x00130000
#define READ_DATA  0x00140000
#define OUTPUT_PROBS  0x00150000
#define INIT_ALLOCATE  0x00160000
#define MAT_INVERT  0x00170000
#define KEYWORDS  0x00180000
#define STD_KEYWORDS  0x00190000
#define CONST_KEYWORDS  0x001a0000
#define FILEN_KEYWORDS  0x001b0000
#define NAMELIST  0x001c0000
#define OUTPUT_FLUXES  0x001d0000
#define OUTPUT_MAGS  0x001e0000
#define OUTPUT_SAMPS  0x001f0000
#define READ_IMAGE  0x00200000
#define READ_SLOPES  0x00210000
#define READ_INTERPS  0x00220000
#define COMPUTE_RES  0x00230000
#define DATE           0x00240000
#define PARSE_ARGS              0x00250000
#define DECODE_ERR_MSSG         0x00260000


/*
    List error codes here.  All must be equal to zero after
    right-shifting 16 bits.
*/

#define STATUS_OK               0x00000000   /* Normal termination. */
#define STATUS_OK_INFO  0x00000003   /* Normal with special info. */
#define MEM_INSUFF  0x00000021   /* Normal with warning. */
#define MAT_SINGULAR  0x00000022
#define TOO_MANY_BAD_PIXELS     0x00000023
#define NOT_A_NUMBER  0x00000024
#define MISSING_INPUT           0x00000040   /* Error condition. */
#define POS_EXPECTED  0x00000041
#define DIVIDE_ZERO  0x00000042
#define NNEG_EXPECTED  0x00000043
#define LOG_NEG   0x00000044
#define ROOT_NEG  0x00000045
#define FOPEN_FAILED  0x00000046
#define COULD_NOT_READ  0x00000047
#define DIM_MISMATCH  0x00000048
#define OUT_OF_RANGE  0x00000049
#define INTERP_FAILED  0x0000004a
#define NO_ARGS   0x0000004b
#define NOT_ODD   0x0000004c
#define UNKNOWN_ERROR           0x0000004d
#define INPUTS_OUT_OF_RANGE     0x0000004e
#define MALLOC_FAILED  0x0000004f
#define PARAM_OUT_OF_RANGE 0x00000050
#define TOO_MANY_DATA_PLANES    0x00000052
#define FITS_MSG_MASK  0x00001000   /* Special FITS error codes. */


