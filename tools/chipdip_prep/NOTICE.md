# Third-party notice — ChIP-DIP

The files in this directory (`Snakefile`, `pipeline_counts.smk`, `pipeline.yaml`,
`chipdip.yaml` and `scripts/`) are a vendored copy of the **ChIP-DIP Snakemake
workflow** developed in the **Guttman Lab at Caltech**, adapted so that
ChromaPilot's `chipdip_prep` tool can invoke it programmatically.

`scripts/java/` contains the `BarcodeIdentification` tool
(`edu.caltech.lncrna.barcode`), also from the Guttman Lab.

These components are **not** covered by ChromaPilot's MIT license. They remain
subject to the license of the upstream ChIP-DIP project, and work that uses them
should cite the ChIP-DIP method paper in addition to ChromaPilot.

> **Maintainers:** add the upstream repository URL, license text and the ChIP-DIP
> citation here before publishing.
