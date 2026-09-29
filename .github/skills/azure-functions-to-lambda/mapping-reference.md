# Trigger and Binding Mapping

| Azure Function Trigger/Binding | AWS Target | Notes |
|---|---|---|
| HTTP trigger | API Gateway + Lambda | Auth model must be reviewed |
| Timer trigger | EventBridge schedule | Preserve cadence and timezone intent |
| Blob trigger | S3 event notification | Validate object filters |
| Queue trigger | SQS event source mapping | Tune batch and visibility timeout |
| Event Grid | EventBridge | Map event schema and filtering |