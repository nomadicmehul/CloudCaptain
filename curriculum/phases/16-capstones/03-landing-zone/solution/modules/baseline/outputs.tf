output "log_bucket_name" {
  description = "Name of the encrypted, private log archive bucket."
  value       = aws_s3_bucket.log_archive.bucket
}

output "log_bucket_arn" {
  description = "ARN of the log archive bucket, the destination for VPC flow logs."
  value       = aws_s3_bucket.log_archive.arn
}
