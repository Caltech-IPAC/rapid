#! /usr/bin/perl -w

# Returns list of FITS images containing only certain keyword settings.
# 4/20/01 V. 7.0 - Fixed readfitshdr bug.                  
# 4/30/02 V. 8.0 - Changed shebang line to not require perl-path 
#                  environment variable, and replaced -h help option
#                  with a tutorial that is printed when the script
#                  is executed sans command-line arguments.
# 3/28/08 V. 9.0 - Made changes to fix the tutorial printout and make 
#                  the code more robust (replaced some deprecated
#                  code and added 'use strict').

use strict;

use Getopt::Std;
use File::Find;

my $debug = 0;

my ($options, %options, $version, $iam, $heading, $i, $filein, @files, 
       @pairs, @notpairs, $verbose, $ncharmax, @keywords, $keywordnow,
       $file, $n, $value, $iflag, $valuenow, $nchar, $icnt);

%options = ();

getopts("hvrk:e:p:i:",\%options);


$version = '9.1';
$iam = 'filterFITSimages';
$heading = "\n${iam}, Version $version\n\n".
           "Returns list of FITS images containing only certain keyword settings,\n".
           "or returns list of FITS images with their values of selected keywords.\n\n".
           "By Russ Laher (laher\@ipac.caltech.edu)\n".
           "Copyright (C) 2019 California Institute of ".
           "Technology\n\n";


if ($options{"r"}) {
   @files = '.' unless @files;
   $i = 0;
   find sub { $files[$i++] = $File::Find::name; }, @ARGV
} else {
   if ($options{"i"}) {
      $filein = $options{"i"};
      open IN, "<$filein" or die "Can't open $filein; quitting";
      $i = 0;
      while ($file = <IN>) {
         chomp $file;
         $file =~ s/^\s*(.*?)\s*$/$1/;
         $files[$i++] = $file;
      }
      close IN;
   } else {
      @files = @ARGV;
   }
}

if (! @files) {
   print "$heading";
   print " Usage:\n";
   print "    filterFITSimages [switches] [options] filename(s)\n";
   print " Switches:\n";
   print "    -r     Process recursively in subdirectories of \n".
         "              specified directory(s).\n";
   print "    -v     Set verbose mode.\n";
   print "    -h     Help; print available options and switches.\n";
   print " Options:\n";
   print "    -k     Keyword1,value1,keyword2,value2, ..., \n".
         "              that the returned images must include.\n";
   print "    -e     Keyword1,value1,keyword2,value2, ..., \n".
         "              that the returned images must exclude.\n";
   print "    -p     Keyword1,keyword2,keyword3, ..., \n".
         "              whose values are to be listed.\n";
   print "    -i     Filename of input list of candidate FITS files.\n";
   print "\nIf the -p option is invoked, then the -k and -e options are \n".
         "deactivated.  If the -i option is used to specify an input list \n".
         "of FITS files, then it's not necessary to specify filename(s) at\n".
         "the end of the command line.  Otherwise, you must enter the \n".
         "filename(s) at the end of the command line (list of space-\n".
         "separated filenames or *), or if -r is invoked, enter directory \n".
         "name(s) (and don't use * in this case; instead, use . for current \n".
         "directory, or specify them explicitly.\n\n";
   die "Quitting... \n\n";
} else {
  if ($debug) {
    $n = @files;
    print "n=$n,$files[0],$files[1]\n";
  }
}



$verbose = $options{"v"};

if (defined $options{"p"}) {
   (@keywords) = split(/\s*,\s*/,$options{"p"});
}
if (defined $options{"k"}) {
   (@pairs) = split(/\s*,\s*/,$options{"k"});
}
if (defined $options{"e"}) {
   (@notpairs) = split(/\s*,\s*/,$options{"e"});
}


# Get column widths.

$ncharmax = 0;
my (@ncharmax);
for ($i = 0; $i < @keywords; $i++) {
  $ncharmax[$i] = 11;   # Minimum width
}
foreach $file (@files) {

   if (! (($file =~ /\.fits$/) or ($file =~ /\.fits\.fz$/))) { 
      if ($verbose) { print "...skipping\n"; }
      next; 
   }

   if (! (-e $file)) { die 'Image not found; quitting'; }
   my %hdr = readfitshdr($file);
   $nchar = length($file);
   if ($nchar > $ncharmax) { $ncharmax = $nchar; }

   for ($i = 0; $i < @keywords; $i++) {
      $keywordnow = $keywords[$i];
      $value = $hdr{$keywordnow};
      $nchar = length($value);
      if ($nchar > $ncharmax[$i]) { $ncharmax[$i] = $nchar; }
      
   }
}

if (defined ($options{'p'})) { 
   printf "| %-13s | %-${ncharmax}s | ","Count","Filename"; 
   for ($i = 0; $i < @keywords; $i++) {
      $keywordnow = $keywords[$i];
      printf "%-${ncharmax[$i]}s | ",$keywordnow; 
   }
   print "\n";
   printf "| %-13s | %-${ncharmax}s | ","i","c"; 
   for ($i = 0; $i < @keywords; $i++) {
      $keywordnow = $keywords[$i];
      printf "%-${ncharmax[$i]}s | ","c"; 
   }
   print "\n";
}

$icnt = 1;

foreach $file (@files) {

   if (! (($file =~ /\.fits$/) or ($file =~ /\.fits\.fz$/))) { 
      if ($verbose) { print "...skipping\n"; }
      next; 
   }

   if (! (-e $file)) { die 'Image not found; quitting'; }
   my %hdr = readfitshdr($file);

   if (defined ($options{'p'})) { 
      printf "  %13d   %-${ncharmax}s   ",$icnt++,$file; 
   }
   for ($i = 0; $i < @keywords; $i++) {
      $keywordnow = $keywords[$i];
      $value = $hdr{$keywordnow};
      if (defined $value) { 
         $value =~ s/^'(.*)/$1/;
         $value =~ s/(.*)'$/$1/;
         $value =~ s/^\s*(.*?)\s*$/$1/;
         printf "%${ncharmax[$i]}s   ",$value; 
      } else {
         printf "%${ncharmax[$i]}s   ","           "; 
      }
   }
   if (defined ($options{'p'})) { print "\n"; next; }

   if ($verbose) {
      print "Processing $file...\n";
   }

   $iflag = 0;
   for ($i = 0; $i < @pairs; $i+=2) {
      $keywordnow = $pairs[$i];
      $valuenow = $pairs[$i+1];

      $value = $hdr{$keywordnow};

      if ($verbose) { 
         print "key,val,valcmp = $keywordnow,$valuenow,$value.\n"; 
      }

      if (defined $value) {
         $value =~ s/^'(.*)/$1/;
         $value =~ s/(.*)'$/$1/;
         $value =~ s/^\s*(.*?)\s*$/$1/;
         if (! ($value eq $valuenow)) {
           $iflag = 1;
         }
      } else {
         if ($verbose) { print "Keyword not found...\n"; }
         $iflag = 1;  
      }

   }

   if ($iflag == 1) { 
      if ($verbose) { print "...skipping\n"; }
      next; 
   }

   $iflag = 0;
   for ($i = 0; $i < @notpairs; $i+=2) {
      $keywordnow = $notpairs[$i];
      $valuenow = $notpairs[$i+1];

      $value = $hdr{$keywordnow};

      $value =~ s/^'(.*)/$1/;
      $value =~ s/(.*)'$/$1/;
      $value =~ s/^\s*(.*?)\s*$/$1/;
 
if ($verbose) { print "key,val,valcmp = $keywordnow,$valuenow,$value.\n"; }

      if (defined $value) {
         if ($value eq $valuenow) {
           $iflag = 1;
         }
      } else {
         if ($verbose) { print "Keyword not found...\n"; }
         $iflag = 1;  
      }

   }

   if ($iflag == 1) { 
      if ($verbose) { print "...skipping\n"; }
      next; 
   }

   if (defined ($options{'k'} || $options{'e'})) { print "$file\n"; }

}

sub readfitshdr {
  my $fits_file = shift;
  my ($card,$retval,%hdr,$key,$val,$comment);

  sysopen FITS,$fits_file,0 or die "*** $fits_file inaccessible; $!";
  while($retval=sysread(FITS,$card,80)) {
    die "*** Premature EOF on $fits_file" if $retval<80;
    die "*** We seem to have run off the header on $fits_file" 
        if $card =~ /[^ -~]/; # Non-printing character found
    last if $card =~ /^END +$/; # End of header
    next if substr($card,8,1) ne "="; # Not a key=value pair
    ($key,$val,$comment) = $card =~ m/^(\S+)\s*=\s*(.*?)\s*(\/\s*(.*)?)?$/;
    $hdr{$key} = $val;
  }
  die "*** IO error on $fits_file: $!" if ! defined $retval;
  close FITS or die "*** IO error on close of $fits_file: $!";

  return %hdr;
}








