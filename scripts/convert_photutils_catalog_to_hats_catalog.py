'''
Usage:

python -m venv ./hats_env
source ./hats_env/bin/activate
which python
python /Users/laher/git/rapid/scripts/convert_photutils_catalog_to_hats_catalog.py
deactivate
'''

import glob
from pathlib import Path
from dask.distributed import Client
from hats_import.catalog.arguments import ImportArguments
from hats_import.pipeline import pipeline_with_client
from hats_import.catalog.file_readers import CsvReader

if __name__ == '__main__':


    hats_catalog_name = "my_hats_catalog"
    output_path = Path.cwd()
    tmp_dir = "tmp"

    # Input path where PhotUtils catalog files are stored with unique filename suffixes.

    test_data_dir = "/Users/laher/Folks/rapid/hats-import-parquet"
    catalog_csv_path = glob.glob(f"{test_data_dir}/sfftdiffimage_masked_psfcat*.txt")

    print(f"catalog_csv_path={catalog_csv_path}")


    # Specify import arguments

    args = ImportArguments(
        ra_column="ra",
        dec_column="dec",
        lowest_healpix_order=2,
        highest_healpix_order=5,
        file_reader=CsvReader(sep=r'\s+'),
        input_file_list=catalog_csv_path,
        output_artifact_name=hats_catalog_name,
        output_path=output_path,
        tmp_dir=tmp_dir,
        tmp_path=tmp_dir,
        resume=False
    )


    # Write HATS catalog.  HATS stands for Hierarchical Adaptive Tiling Scheme.

    with Client(n_workers=1) as client:
        pipeline_with_client(args, client)
