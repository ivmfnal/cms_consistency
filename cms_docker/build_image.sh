#!/bin/sh

docker build -t cmssw/rucio-consistency image
docker push cmssw/rucio-consistency 
