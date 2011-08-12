#!/bin/bash

# Script (hacky) to help with releasing a new version of the openblock packages.
# This assumes that all the packages have the same version number.

HERE=`(cd "${0%/*}" 2>/dev/null; echo "$PWD"/)`
SOURCE_ROOT=`cd $HERE/../.. && pwd`

export VERSION=$1

if [ -z "$VERSION" ]; then
    echo Need to specify the new version.
    exit 1
fi

cd $SOURCE_ROOT
PKGS="ebpub ebdata obadmin obdemo"
for PKGDIR in $PKGS; do
    cd $SOURCE_ROOT/$PKGDIR
    sed -i -e "s/VERSION=.*\$/VERSION=\"${VERSION}\"/" setup.py
    python setup.py sdist > /dev/null || exit 1
    FULLNAME=`python setup.py --fullname`
    echo "Full package name: $FULLNAME"
    python setup.py --classifiers | grep -i status
    if [ -e dist/${FULLNAME}.tar.gz ]; then
	echo package is at $PKGDIR/dist/${FULLNAME}.tar.gz
	echo
    else
	echo Failed to build $FULLNAME sdist. Try re-running python setup.py sdist to see why
	exit 1
    fi
done

echo Building html docs
cd $SOURCE_ROOT/docs
sed -r -i -e "s/^release[[:space:]]*=[[:space:]]* ['\"].*['\"].*/release = \'${VERSION}\'/" conf.py
make -s clean html SPHINXOPTS=-q || exit 1
cd $SOURCE_ROOT

echo
echo Building composite requirements file
REQFILE=openblock-requirements-${VERSION}.txt
cd $SOURCE_ROOT

echo "# AUTO-GENERATED" > $REQFILE
echo "# Pip requirements for installing all OpenBlock ${VERSION} packages" >> $REQFILE
echo >> $REQFILE
find . -name requirements.txt | xargs cat | sort | grep -v "^#" | uniq >> $REQFILE

echo "Note, the requirements file won't work until you push your openblock packages"
echo "to pypi (or comment them out from the file)"
echo "Note also that it's sorted alphabetically, so packages that are sensitive"
echo "to installation order may be problematic."
for PKGDIR in $PKGS; do
    echo ${PKGDIR}==${VERSION} >> $REQFILE
done

echo Output is at $REQFILE
