export DOCKER=nvidia-docker
export TAG='himkt/pyner:latest'
export PWD=`pwd`
export USERID=`id -u`
export USERGROUPID=`id -g`
export SUDO='sudo'


.PHONY: build start test lint

build:
	$(SUDO) $(DOCKER) build \
		-t $(TAG) . \
		--build-arg GID=$(USERGROUPID) \
		--build-arg UID=$(USERID) \
		--file=docker/Dockerfile

start:
	$(SUDO) $(DOCKER) run \
		--user $(USERID):$(USERID) \
		--volume $(PWD)/data:/home/docker/data \
		--volume $(PWD)/model:/home/docker/model \
		-it $(TAG)

test:
	python -m unittest discover

lint:
	flake8 pyner

get-glove:
	cd data && wget http://nlp.stanford.edu/data/glove.6B.zip
	cd data && unzip glove.6B.zip
	cd data && rm glove.6B.zip

get-lample:
	cd data/processed/lample_embeddings && bash download.sh && rm cookie.txt
	python pyner/tool/vector/prepare_embeddings.py \
			data/processed/lample_embeddings/skipngram_100d.txt \
			data/processed/lample_embeddings/skipngram_100d \
			--format word2vec
