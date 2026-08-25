# Clone the repo and switch to the branch (if not already)
git clone https://github.com/Amls-s/arepo.git
cd arepo
git checkout add-kenya-schools-data

# Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Upgrade packaging tools
python -m pip install --upgrade pip setuptools wheel

# Install Python deps from requirements.txt
python -m pip install -r requirements.txt

# Run the pipeline
python3 merge_kenya_schools.py

# Inspect outputs
ls -lh kenya_schools_cleaned.*
head -n 10 kenya_schools_cleaned.csv
