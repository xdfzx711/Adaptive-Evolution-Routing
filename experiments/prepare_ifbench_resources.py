"""Explicitly prepare official NLTK data before starting IFBench workers."""

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile


NLTK_DATA_COMMIT = "550b6625bcef1f2abff2ff770a5a0d272c9c6b2a"
PACKAGES = ("tokenizers/punkt", "tokenizers/punkt_tab", "corpora/stopwords",
            "taggers/averaged_perceptron_tagger_eng")


def prepare_resources(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for resource in PACKAGES:
        url = f"https://raw.githubusercontent.com/nltk/nltk_data/{NLTK_DATA_COMMIT}/packages/{resource}.zip"
        with urlopen(url, timeout=120) as response:
            data = response.read()
        destination = root / resource.split("/")[0]
        with ZipFile(BytesIO(data)) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("NLTK archive entry escapes resource directory")
            archive.extractall(destination)
        records.append({"resource": resource, "url": url, "archive_sha256": sha256(data).hexdigest()})
        print("Prepared " + resource, flush=True)
    (root / "PROVENANCE.json").write_text(json.dumps({"commit": NLTK_DATA_COMMIT,
        "resources": records}, indent=2) + "\n", encoding="utf-8")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".aer-cache/ifbench/nltk_data")
    print(prepare_resources(parser.parse_args().directory))


if __name__ == "__main__":
    main()
