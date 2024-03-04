FROM redhat/ubi9

# Install python.

WORKDIR /shared
RUN curl -O https://repo.anaconda.com/archive/Anaconda3-2023.09-0-Linux-x86_64.sh
RUN chmod +x Anaconda3-2023.09-0-Linux-x86_64.sh
RUN ./Anaconda3-2023.09-0-Linux-x86_64.sh -b -p /shared/anaconda3

# Install make command.

RUN yum -y install make

# Install gcc command.

RUN yum -y install gcc

# Install perl.

ADD http://www.cpan.org/src/5.0/perl-5.38.2.tar.gz /shared
RUN tar -xzf perl-5.38.2.tar.gz

WORKDIR /shared/perl-5.38.2
RUN ./Configure -des -Dprefix=/shared/perl -Dcc=/bin/gcc

RUN make
#RUN make test; exit 0
RUN make install

WORKDIR /root
RUN echo "alias which=\"command -v\"" >> /root/.bash_profile
RUN echo "export PYTHONPATH=/shared/anaconda3" >> /root/.bash_profile
RUN echo "export PATH=/shared/anaconda3/bin:/shared/perl/bin:$PATH" >> /root/.bash_profile

# Install python asdf package.

RUN /shared/anaconda3/bin/pip install asdf

# Install gsl library.

RUN yum -y install wget

WORKDIR /shared/gsl

RUN wget ftp://ftp.gnu.org/gnu/gsl/gsl-2.7.tar.gz

RUN ./configure --prefix=/shared/gsl
RUN make
RUN make check
RUN make install