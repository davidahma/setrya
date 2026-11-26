# Setrya control mappings

**Public draft · awaiting auditor review**

The [2026-11-26 draft](2026-11-26-draft.1.json) maps individual Setrya observations to control references. Each row gives the framework, edition, control, source, and the circumstances in which that evidence could support an assessment. The assessor decides whether the control is met.

| Framework and edition | Reference | Evidence in the draft |
| --- | --- | --- |
| AICPA 2017 Trust Services Criteria with 2022 revised points of focus | C1.2 | Direct observation of disposing of a declared confidential record. |
| ISO/IEC 27001:2022 | A.8.10 | Deleting a known copy, rewriting a derivative, and checking whether a deferred writer brings it back. |
| PCI DSS 4.0.1 | 3.2.1 | Scoped account-data disposal and evidence of a declared retention exception. A separate row records the historical reference in retired PCI DSS 3.2.1. |
| Regulation (EU) 2016/679 | Article 17(1), 17(3) | Deletion and readback of declared copies, plus preservation of a distinct retained record. The case owner establishes whether the right or exception applies. |

The examples in the [OSCAL guide](../oscal.md) use fictional references. To try this draft, explicitly supply the versioned JSON with `--mapping` and use a local Assessment Plan with matching control IDs. Those IDs are Setrya's keys for the mapping. Use the corresponding IDs from your assessor's plan and catalog when adapting the draft. The export records the exact mapping version and SHA-256 digest.

For review, cite the mapping version, digest, and row. Check the authority and edition, the decision the observation can support, and which evidence the assessor still needs. A qualified reviewer can approve a specific version and scope. Publish any approved revision as a new version with its review record.
