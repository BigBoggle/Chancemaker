# UKY CS498 Senior Project - Chancemaker

```text
           ,--,        Chancemaker - a Prediction Engine and Database
     _ ___/ /\|        for Horse Racing.
 ,;'( )__, )  ~
//  //   '--; 
'   \     | ^
     ^    ^
```
[![Python CI](https://github.com/BigBoggle/Chancemaker/actions/workflows/python-app.yml/badge.svg)](https://github.com/BigBoggle/Chancemaker/actions/workflows/python-app.yml)

## Project Members:
- Ezekiel Boggs
- Jace Bennett
- Jameson Wells
- Andrew Hostettler
- Nathan Herrington

## Environment Setup

### Dev Environment
```shell
# All of the dependencies are in the pyproject.toml
python3 -m venv .venv
source .venv/bin/activate
pip install -e .'[dev]'
pre-commit install

# Or if direnv is installed
direnv allow

```
