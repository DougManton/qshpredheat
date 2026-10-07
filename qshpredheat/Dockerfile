ARG BUILD_FROM
FROM $BUILD_FROM

COPY requirements.txt /
RUN pip3 install --no-cache-dir -r /requirements.txt

COPY run.sh /
COPY main.py /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
