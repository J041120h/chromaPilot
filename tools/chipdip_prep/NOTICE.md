# Third-party notice — ChIP-DIP

The files in this directory (`Snakefile`, `pipeline_counts.smk`, `pipeline.yaml`,
`chipdip.yaml` and `scripts/`) are a vendored copy of the **ChIP-DIP Snakemake
workflow** from the Guttman Lab at Caltech, adapted so ChromaPilot's
`chipdip_prep` tool can invoke it programmatically.

`scripts/java/` contains the `BarcodeIdentification` tool
(`edu.caltech.lncrna.barcode`) from the same project.

* **Upstream:** <https://github.com/GuttmanLab/chipdip-pipeline>
* **License:** MIT — `Copyright (c) 2023 GuttmanLab` (reproduced below)

## Citation

Work that uses `chipdip_prep` should cite the ChIP-DIP method paper in addition
to ChromaPilot:

> Perez AA, Goronzy IN, Blanco MR, Yeh BT, Guo JK, Lopes CS, Ettlin O, Burr A,
> Guttman M. ChIP-DIP maps binding of hundreds of proteins to DNA simultaneously
> and identifies diverse gene regulatory elements. *Nature Genetics.* 2024
> Dec;56(12):2827–2841. doi:[10.1038/s41588-024-02000-5](https://doi.org/10.1038/s41588-024-02000-5)

## License

```
MIT License

Copyright (c) 2023 GuttmanLab

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
