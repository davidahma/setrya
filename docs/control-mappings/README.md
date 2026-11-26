# Setrya control mappings

**Public draft · awaiting auditor review**

The [2026-11-26 second draft](2026-11-26-draft.2.json) maps individual Setrya observations to control references. Each row gives the framework, edition, control, source, and the circumstances in which that evidence could support an assessment. The assessor decides whether the control is met. The [first draft](2026-11-26-draft.1.json) remains available for exports that pin its digest.

| Framework and edition | Reference | Evidence in the draft |
| --- | --- | --- |
| AICPA 2017 Trust Services Criteria with 2022 revised points of focus | C1.2 | Direct observation of disposing of a declared confidential record. |
| AICPA 2017 Trust Services Criteria with 2022 revised points of focus | P4.3 | A scoped personal-data disposal check or direct readback for an engagement that includes the privacy criteria. |
| ISO/IEC 27001:2022 | A.8.10 | Deleting a known copy, rewriting a derivative, and checking whether a deferred writer brings it back. |
| ISO/IEC 27701:2025, second edition | A.1.4.9 | Disposal checks under Annex A, Table A.1 for PII controllers, when the assessment scope includes the control. |
| PCI DSS 4.0.1 | 3.2.1 | Scoped account-data disposal and evidence of a declared retention exception. A separate row records the historical reference in retired PCI DSS 3.2.1. |
| Regulation (EU) 2016/679 | Article 17(1), 17(3) | Deletion and readback of declared copies, API-visible readback after reported deletion, and preservation of a distinct retained record. The case owner establishes whether the right or exception applies. |
| HIPAA Security Rule, 45 CFR Part 164 Subpart C | 164.310(d)(2)(i) | Synthetic evidence about particular application copies in a declared final-disposition procedure. Evaluate hardware and media handling using separate evidence. |

Check the current text, the control selected in the assessment, and its applicability before relying on a draft row. [AICPA Privacy Criteria](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022) · [ISO/IEC 27701:2025](https://www.iso.org/standard/27701) · [BSI edition crosswalk](https://www.bsigroup.com/siteassets/pdf/th-th/products-and-services/th-as-cross-lg-ict-dt-iso27701-mp-iso27701revisionwebinar-1125.pdf) · [45 CFR 164.310](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C/section-164.310)

The examples in the [OSCAL guide](../oscal.md) use fictional references. To try this draft, explicitly supply the versioned JSON with `--mapping` and use a local Assessment Plan with matching control IDs. Those IDs are Setrya's keys for the mapping. Use the corresponding IDs from your assessor's plan and catalog when adapting the draft. The export records the exact mapping version and SHA-256 digest.

For review, cite the mapping version, digest, and row. Check the authority and edition, the decision the observation can support, and which evidence the assessor still needs. A qualified reviewer can approve a specific version and scope. Publish any approved revision as a new version with its review record.
