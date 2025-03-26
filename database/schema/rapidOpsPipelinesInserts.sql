--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsPipelinesInserts
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 September 2024
--------------------------------------------------------------------------------------------------------------------------

INSERT INTO pipelines (ppid,priority,script,descrip) values (15,3,'awsBatchSubmitJobs_launchSingleSciencePipeline.py', 'Science pipeline for input SCA image.');
INSERT INTO pipelines (ppid,priority,script,descrip) values (12,4,'generateReferenceImage.py', 'Standard reference-image pipeline.');
INSERT INTO pipelines (ppid,priority,script,descrip) values (17,5,'awsBatchSubmitJobs_launchSinglePostProcPipeline.py', 'Post-processing pipeline for input SCA image.');
