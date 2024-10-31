use Digest::MD5;

use strict;

my $status = 1;
my %fids = ("184" => 1, "158" => 2,"129" => 3,"213" => 4,"062" => 5,"106" => 6,"087" => 7, "146" => 8);

my $sqlfile = "psfs.sql";
open(OUT, ">$sqlfile");

opendir(THISDIR, "/Users/laher/Folks/rapid/psfs/PSFs");
my @files=sort readdir THISDIR;
closedir THISDIR;

foreach my $f (@files) {
    next if ($f =~ m/^\./);

    my ($sca, $fnum) = $f =~ /^WFI_SCA(\d+)_F(\d+)_PSF_DET_DIST.fits$/;
    my $fid = $fids{$fnum};

    my $outfile = "PSFs/" . $f;
    if (! open(FILE, "<$outfile") ) {
        print "*** Couldn't open $outfile for reading; $!\n";
        exit(64);
    }
    binmode(FILE);
    my $checksum = Digest::MD5->new->addfile(*FILE)->hexdigest;
    if (! close(FILE) ) {
        print "*** Couldn't close $outfile; $!\n";
        exit(64);
    }

    my $s3f = "s3://rapid-pipeline-files/psfs/" . $f;

    #`aws s3 cp PSFs/$f $s3f`;

    $sca += 0;    # Remove leading zero.

    print "$fid,$sca,$s3f,$checksum,$status\n";

    my $q = "select * from addPSF(" .
            "cast($fid as smallint)," .
            "cast($sca as smallint)," .
            "cast('$s3f' as character varying(255))," .
            "cast('$checksum' as character varying(32))," .
            "cast($status as smallint)) as " .
            "(psfid integer," .
            " version smallint);";

    print OUT "$q\n";
}

close(OUT);

exit 0;
