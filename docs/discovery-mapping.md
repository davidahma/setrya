# Map a Macie finding to a test case

A Macie finding identifies an S3 object. One object can contain data about several people. An application owner reviews the finding and names the disposable subject, source record, permitted action, and services Setrya should check. Keep the reviewed mapping in a trusted checkout or case system. Importing a finding reads a local file and requires no AWS credentials.

```sh
setrya discovery init --output incident
setrya discovery import --finding incident/finding.json --output macie-import.json
```

The starter includes a synthetic [EventBridge finding](../setrya/discovery/macie-example.json) and its [complete mapping](../setrya/discovery/mapping-example.json). The finding and the mapping are separate inputs. Approve the mapping against the source of record before using it with an application fixture.

## What the import contains

Supply a raw Macie 1.0 classification finding, a single-finding `GetFindings` response, or its `aws.macie` EventBridge event. A sensitive-data finding must report a complete scan and `archived: false`. The reader accepts UTF-8 JSON files up to 1 MiB with unique object fields. The example command writes a `setrya.discovery.macie` version 1 object. [Macie event examples](https://docs.aws.amazon.com/macie/latest/user/findings-publish-event-schemas.html) · [AWS field reference](https://docs.aws.amazon.com/macie/latest/APIReference/findings-describe.html)

| Field | Meaning |
| --- | --- |
| `catalog` | `aws-macie` identifies the originating discovery service. |
| `sample` | A Macie sample uses fictitious resources. The value is `true` for a sample, `false` when AWS explicitly marks it as an ordinary finding, and `null` when the export omits the indicator. This describes the upstream finding. The Setrya drill still uses a disposable application fixture. [AWS sample findings](https://docs.aws.amazon.com/macie/latest/user/findings-samples.html) |
| `finding_id` | Macie's finding ID. The EventBridge delivery has its own ID and can be delivered again. |
| `asset_sha256` | Fingerprint of the AWS partition, account, region, bucket ARN, exact object key, S3 version ID and ETag. At least a concrete version ID or an ETag must be available. |
| `finding_type`, `categories` | Macie's finding type and sorted, distinct coarse categories reported in the result. A positive custom-identifier count adds `CUSTOM_IDENTIFIER`. An empty list means the export supplied no recognized category. Custom rule names, individual detections and occurrence coordinates stay in the original finding. |
| `observed_at` | The finding's `createdAt`, normalized to UTC. This dates the reported discovery. The export does not establish when the AI application last read the object. |
| `finding_sha256` | Digest of the complete inner finding, including fields the import omits. Sorting JSON keys makes this stable across formatting changes and supported delivery wrappers. |

A reviewer with the original AWS export can reproduce both digests offline. The public import contains neither the bucket name nor the object key. Digests remain linkable and someone who knows the object details can compute its fingerprint. Treat the import and case identifiers according to your handling policy.

To calculate an approved pointer from a known object, call `asset_fingerprint(partition, account, region, bucket_arn, key, version_id, etag)` from `setrya.discovery.macie`. The digest uses SHA-256 over the compact, ASCII-escaped JSON array `["setrya.macie.asset.v1", partition, account, region, bucket_arn, key, version_id, etag]`. Use an empty string for an absent version ID or ETag. Preserve the object key and generation fields exactly as Macie supplied them.

## What the application owner approves

The `setrya.discovery.mapping` version 1 manifest binds a single case to an expected finding ID, asset fingerprint, finding type and exact `observed_at` value. A new observation or an object with a different version requires an updated mapping. From the privately authenticated original export, take the catalog, sample indicator, finding digest, and sorted category list from the normalized file. Pin all four in the reviewed mapping, including an empty list when the finding supplies no recognized category. A normalized file alone cannot establish who created it. The mapping check rejects a different digest or category even when the asset and finding ID agree.

| Fields | What to supply |
| --- | --- |
| `case_id`, `subject_ref`, `classification` | A case pointer, the tenant-scoped subject HMAC used by the fixture, and the customer's own handling label. Use opaque references that a reviewer can safely share. |
| `environment`, `revision` | The disposable deployment identifier and the full Git commit ID of the application under test. |
| `conversation`, `route`, `callers`, `questions` | The saved conversation, its answer route, one or more distinct authenticated caller aliases, and the digests of the target and control questions in that order. `SharedCase.spec()` supplies the question digests. |
| `source` | The exact application `store`, `key`, source `revision`, and approved `action`. The application source revision and the S3 object generation are separate identifiers. |
| `services` | Every service expected to participate, including services awaiting instrumentation. Give each a unique `id`, `role` and accountable `owner`. |

Direct service roles are `source`, `summary`, `checkpoint`, `cache`, `memory`, `vector`, and `store`. Declare the exact store and key and choose `delete`, `rewrite`, or `preserve`. At least one `source` row must match the approved source. A rewrite keeps the same key, removes the target fact and preserves the control fact. The `endpoint` row names the route. A `provider` or `external` row names its store and key and takes the `external` action. Required application stores cannot be reclassified as external exceptions within the same mapping.

For a retained source, set `action` to `retain` in both the source and its service row. Supply the row's owner, an opaque `evidence` reference for the policy, and a `follow_up` date in YYYY-MM-DD format. Setrya records the open policy item without invoking the fixture. No deletion was exercised.

Use trusted fixture code for service observations and application changes. Setrya compares the mapping with the fixture identity, baseline source revision, and evidence. A classification label alone grants no authority to delete or rewrite an object.
