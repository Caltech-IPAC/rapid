RAPID Tests Conducted For Development
####################################################

The tests described below are organized by processing date.

Here are Perl one-liners to query the operations database 
for performance results::

    perl -e 'print"count,nframes,startedhours,elapsedseconds\n";$q="select nframes,extract(day from started) * 24.0 + extract(hour from started) + extract(minute from started)/60.0 + extract(second from started)/3600.0-200.954635724166 as startedhours, extract(hour from elapsed)*3600 + extract(minute from elapsed)*60 + extract(second from elapsed) as elapsedseconds from jobs a, diffimages b, diffimmeta c, refimmeta d where a.rid=b.rid and a.ppid=15 and b.pid=c.pid and b.vbest>0 and b.rfid=d.rfid and exitcode=0 order by started; "; @op=`/usr/local/opt/postgresql\@15/bin/psql -h 35.165.53.98 -d rapidopsdb -p 5432 -U rapidporuss -c \"$q\"`; $i=0;shift @op; shift @op; foreach my $op (@op) {   if ($op =~ /row/) { last; }    chomp $op;       $op =~ s/^\s+|\s+$//g;    my (@f) = split(/\s*\|\s*/, $op);  $nframes = $f[0]; $startedhours = $f[1];   $elapsedtimeseconds = $f[2]; $i++;  print"$i,$nframes,$startedhours,$elapsedtimeseconds\n"; }' >& elapsed.txt
    perl -e 'print"count,nframes,launchedhours,qwaitedseconds\n";$q="select nframes,extract(day from launched) * 24.0 + extract(hour from launched) + extract(minute from launched)/60.0 + extract(second from launched)/3600.0-200.954635724166 as launchedhours, extract(hour from qwaited)*3600 + extract(minute from qwaited)*60 + extract(second from qwaited) as qwaitedseconds from jobs a, diffimages b, diffimmeta c, refimmeta d where a.rid=b.rid and a.ppid=15 and b.pid=c.pid and b.vbest>0 and b.rfid=d.rfid and exitcode=0 order by launched; "; @op=`/usr/local/opt/postgresql\@15/bin/psql -h 35.165.53.98 -d rapidopsdb -p 5432 -U rapidporuss -c \"$q\"`; $i=0;shift @op; shift @op; foreach my $op (@op) {   if ($op =~ /row/) { last; }    chomp $op;       $op =~ s/^\s+|\s+$//g;    my (@f) = split(/\s*\|\s*/, $op);  $nframes = $f[0]; $launchedhours = $f[1];   $qwaitedtimeseconds = $f[2]; $i++;  print"$i,$nframes,$launchedhours,$qwaitedtimeseconds\n"; }' >& qwaited.txt


4/28/2025
************************************

Standard large test run, with all reference images cleared from database 
(status=0 for vbest>0).  AWS Batch machines for science-pipeline jobs
have 2 vCPUs and 16 GB memory.

.. code-block::

    export STARTDATETIME="2028-09-08 04:00:00"
    export ENDDATETIME="2028-09-08 08:30:00"
    python3.11 /code/pipeline/awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.py >& awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange_jid_ge_2_le_90.out &
