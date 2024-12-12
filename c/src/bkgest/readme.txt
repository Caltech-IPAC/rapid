
12/12/24

Build bkgest on laptop, using libraries in rapid_abandon_20240426...

 printenv | grep RAPID
RAPID_SW=/Users/laher/git/rapid

(base) laher [Thu Dec 12 07:43:41] [~/git/rapid/c/src/bkgest] $ printenv PATH
/Users/laher/anaconda3/bin:/Users/laher/anaconda3/condabin:/Users/laher/Software/ImageMagick/ImageMagick-7.0.10/bin:/Users/laher/anaconda3/bin:/Users/laher/Documents/Thoth/dist/jars:/Users/laher/Documents/AperturePhotometryTool/dist/jars:/Users/laher/Software/ant/apache-ant-1.9.16/bin:/Users/laher/git/neid/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/X11/bin:/Library/Apple/usr/bin


export RAPID_SW=~/git/rapid_abandon_20240426
export DYLD_LIBRARY_PATH=~/git/rapid_abandon_20240426/c/lib
export PATH=~/git/rapid_abandon_20240426/c/bin:$PATH

cd /Users/laher/git/rapid/c/src/bkgest
make





(base) laher [Thu Dec 12 13:28:53] [~/Folks/rapid] $ bkgest
Usage: bkgest
       -n <input_namelist_fname> (Optional)
       -i <input_image_fname> (Namelist or required)
       -m <input_mask_fname> (Optional)
       -c <clippedmean_calc_type> (1=Local, 2=Global, 3=Both)
       -o1 <output_clippedmean_fits_fname> (Depends on -c option, required if -f 1 or -f 3 is specified)
       -o2 <output_input-clippedmean_fits_fname> (Depends on -c option, required if -f 2 or -f 3 is specified)
       -o3 <output_sky_scale_fits_fname> (Depends on -c option, required if -f 1 or -f 3 is specified)
       -ot <output_global_clippedmean_data_fname> (Depends on -c option, required if -f 2 or -f 3 is specified)
       -l <log_fname> (Default is stdout)
       -w <local_clippedmean_input_window> (Depends on -c option; pixels on a side; default is 7 pixels)
       -g <local_clippedmean_grid_spacing> (Depends on -c option; computational grid spacing; default is 16 pixels)
       -b <fatal_bit_mask> (Optional, default is zero)
       -bl <integer_percent_of_local_clippedmean_number_bad_pixels_tolerated> (Optional, must be an integer 99% or less, default is 50%)
       -bg <integer_percent_of_global_clippedmean_number_bad_pixels_tolerated> (Optional, must be an integer 99% or less, default is 50%)
       -f <output_image_type>  (1=ClippedMean, 2=ClippedMean-Input, 3=Both, 4=None; default is 1)
       -p <data_plane_to_process> (1=All, 2=First, 3=Last; default is 1)
       -e <pothole> (Optional image value at and below which to ignore)
       -a <ancillary_file_path> (Optional)
       -d (Prints debug statements)
       -v (Verbose output)
       -vv (Super-verbose output)

bkgest_parse_args: Missing input FITS filename (-i <fname>).
bkgest_parse_args: Missing output FITS filename (-o1 <fname>).
bkgest_parse_args: Missing output FITS filename (-o3 <fname>).
bkgest_parse_args: Mask mask = 0
bkgest_log_writer: Log File = stdout
bkgest_log_writer: Input Image File =
bkgest_log_writer: Printing Log output to stdout

Program bkgest, Version 1.3
Mask mask= 0
Calculation Type = 1
Local-ClippedMean Input Window (pixels) = 7
Local-ClippedMean Grid Spacing (pixels) = 16
Percentage of Bad Pixels Tolerated for Local ClippedMean = 50
Percentage of Bad Pixels Tolerated for Global ClippedMean = 50
Image Pothole Value = -1.797690e+308
Data-Plane Flag = 1
Output Image Type = 1
Ancillary Data-File Path = .
Verbose flag = 0
Super-verbose flag = 0
Debug flag = 0
Set-up to do local-clippedmean image calculation.
*** BKE_log_writer: Could not open bkgest_errcodes.h
bkgest Status Message      0xefff
ERRCODE_FILE_NOT_FOUND from Function 0x0000: LOG_WRITER
A total of        0   NaN's were produced in the results.
Processing time: 0.007865 seconds
Current date/time: Thu Dec 12 13:28:55 2024
Program bkgest, version 1.3, terminated.





time bkgest -i diffimage_masked.fits -f 3 -c 1 -g 100 -w 201 -o1 localClippedMean.fits -o2 backgroundSubtracted.fits -o3 skyScale.fits -a /Users/laher/git/rapid_abandon_20240426/c/include
echo $?

Processing time: 6.468094 seconds

APT.csh -i backgroundSubtracted.fits

After converting to clipped mean with sigma = 3.0:

Image: backgroundSubtracted.fits [1:1]

 Image-Data Statistics (data units = D.N.):
 Min. & Max.               = -52492.730469, 99745.148438
 Mean & Standard Deviation = -0.120902, 119.262173
 Median & Scale            = -0.106723, 22.080056
 1 & 99 Percentiles        = -52.949461, 53.380033
 Samples & NaNs/Infs       = 12164774, 4555147
 Skewness & Kurtosis       = 100.386605, 128458.773341
 Jarque-Bera Test          = 8364142153345800.000000


APT.csh -i localClippedMean.fits

Image: localClippedMean.fits [1:1]

 Image-Data Statistics (data units = D.N.):
 Min. & Max.               = 369.554230, 383.192566
 Mean & Standard Deviation = 376.657920, 0.585650
 Median & Scale            = 376.634277, 0.324921
 1 & 99 Percentiles        = 374.828979, 378.466971
 Samples & NaNs/Infs       = 12166932, 4552989
 Skewness & Kurtosis       = 0.972052, 17.887117
 Jarque-Bera Test          = 164115939.960197
