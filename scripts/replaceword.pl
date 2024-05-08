#! /usr/bin/perl

@files = @ARGV;

$word1 = shift @files;
$word2 = shift @files;

$ncounttot = 0;

foreach $file (@files) {

    if (-d $file) {
       next;
    }

   $all = `cat $file`;

   $ncount = ($all =~ s/$word1/$word2/g);

   $ncounttot += $ncount;

   open(OUT,">$file") or die "Couldn't open $file; quitting\n";
   print OUT "$all";
   close(OUT) or die "Couldn't close $file; quitting\n";

}

print "Number of replacements: $ncounttot\n";
