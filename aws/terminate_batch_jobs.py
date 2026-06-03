"""Terminate AWS Batch jobs in specified states."""

import argparse
import boto3


TERMINATABLE_STATES = ["SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"]


def list_jobs(client, job_queue, state):
    jobs = []
    paginator = client.get_paginator("list_jobs")
    for page in paginator.paginate(jobQueue=job_queue, jobStatus=state):
        jobs.extend(page["jobSummaryList"])
    return jobs


def terminate_jobs(client, jobs, reason, dry_run=False):
    for job in jobs:
        job_id = job["jobId"]
        job_name = job["jobName"]
        status = job.get("status", "?")
        if dry_run:
            print(f"[dry-run] Would terminate {job_id} ({job_name}) [{status}]")
        else:
            client.terminate_job(jobId=job_id, reason=reason)
            print(f"Terminated {job_id} ({job_name}) [{status}]")


def main():
    parser = argparse.ArgumentParser(description="Terminate AWS Batch jobs by state")
    parser.add_argument("--queue", required=True, help="Job queue name or ARN")
    parser.add_argument(
        "--states",
        nargs="+",
        default=["RUNNING"],
        choices=TERMINATABLE_STATES,
        metavar="STATE",
        help=f"Job states to terminate. Choices: {TERMINATABLE_STATES}. Default: RUNNING",
    )
    parser.add_argument(
        "--reason",
        default="Terminated by script",
        help="Reason for termination",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List jobs that would be terminated without actually terminating them",
    )
    parser.add_argument("--region", help="AWS region (overrides default)")
    parser.add_argument("--profile", help="AWS profile name")
    args = parser.parse_args()

    session = boto3.Session(
        region_name=args.region,
        profile_name=args.profile,
    )
    client = session.client("batch")

    total = 0
    for state in args.states:
        jobs = list_jobs(client, args.queue, state)
        print(f"Found {len(jobs)} job(s) in state {state}")
        terminate_jobs(client, jobs, reason=args.reason, dry_run=args.dry_run)
        total += len(jobs)

    action = "Would terminate" if args.dry_run else "Terminated"
    print(f"\n{action} {total} job(s) total.")


if __name__ == "__main__":
    main()
