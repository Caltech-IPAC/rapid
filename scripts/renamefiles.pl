#! /usr/bin/perl 

@files = @ARGV;

$word1 = shift @files;
$word2 = shift @files;

foreach $file (@files) {

$fileout = $file;
$fileout =~ s/$word1/$word2/g;

   if ($fileout ne $file) {
      `mv $file $fileout`;
   }

}

