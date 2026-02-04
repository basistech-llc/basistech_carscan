import boto3
import tomllib
import argparse
import sys
import re

def get_bucket_from_samconfig(config_path="samconfig.toml"):
    """Extracts ImageBucketName from samconfig.toml parameter_overrides."""
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        params_str = config['default']['deploy']['parameters'].get('parameter_overrides', "")
        match = re.search(r'ImageBucketName="([^"]+)"', params_str)
        if match:
            return match.group(1)
        return None
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Error reading config: {e}")
        return None

def enable_eventbridge(bucket_name, region, origin):
    """Enables EventBridge notifications for the specified S3 bucket."""
    s3 = boto3.client('s3', region_name=region)

    try:
        s3.put_bucket_notification_configuration(
            Bucket=bucket_name,
            NotificationConfiguration={
                'EventBridgeConfiguration': {}
            }
        )
        print("Success: S3 events are now being sent to EventBridge.")

        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name,
            LifecycleConfiguration={
                'Rules': [{
                    'ID': 'DeleteOldScansOnly',
                    'Status': 'Enabled',
                    'Filter': {'Prefix': 'uploads/'},
                    'Expiration': {'Days': 365}
                }]
            }
        )
        print("Done: Lifecycle rule set (uploads/ expire after 365 days).")

        cors_configuration = {
            'CORSRules': [{
                'AllowedHeaders': ['*'],
                'AllowedMethods': ['POST', 'GET', 'PUT', 'HEAD'],
                'AllowedOrigins': [origin],
                'ExposeHeaders': ['ETag'],
                'MaxAgeSeconds': 3000
            }]
        }
        s3.put_bucket_cors(Bucket=bucket_name, CORSConfiguration=cors_configuration)
        print(f"Done: CORS policy applied for {origin}.")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enable EventBridge notifications on an S3 bucket.")
    parser.add_argument("--bucket", help="S3 bucket name (overrides samconfig.toml)")
    parser.add_argument("--origin", default="https://carscan.nitroba.com", help="Allowed origin for CORS")
    parser.add_argument("--config", default="samconfig.toml", help="Path to samconfig file")
    parser.add_argument("--region", default="us-east-1", help="AWS Region")

    args = parser.parse_args()

    # Resolution logic: CLI Arg > samconfig.toml
    target_bucket = args.bucket or get_bucket_from_samconfig(args.config)

    if not target_bucket:
        print("Error: Bucket name not found in CLI arguments or samconfig.toml.")
        sys.exit(1)

    enable_eventbridge(target_bucket, args.region, args.origin)
