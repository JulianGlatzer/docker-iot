export CERTBOTDIR=/data/certbot
. ${PWD}/.env \
    && echo "starting nginx on port 81" \
    && docker run --name nginx-letsencrypt --rm -v ${CERTBOTDIR}/www:/var/www/certbot:ro -v ${PWD}/nginxletsencrypt.conf:/etc/nginx/conf.d/default.conf:ro -d -p 81:81 nginx:1.15-alpine  \
    && echo "nginx started, now starting certbot" \
    && docker run --name certbot --rm -v ${CERTBOTDIR}/conf/:/etc/letsencrypt:rw -v ${CERTBOTDIR}/www:/var/www/certbot:rw -v ${CERTBOTDIR}/log:/var/log/letsencrypt:rw \
    certbot/certbot:arm32v6-latest \
    certonly --webroot -w /var/www/certbot --agree-tos --email ${CERTBOT_EMAIL} --domain ${CERTBOT_DOMAIN} --domain ${CERTBOT_DOMAIN2} --domain ${CERTBOT_DOMAIN3} --domain ${CERTBOT_DOMAIN4} --noninteractive  | tee log.txt \
    && echo "all done, now do chmod magic..." \
    && find ${CERTBOTDIR}/conf/ -name fullchain.pem -exec chmod 644 {} \; \
    && find ${CERTBOTDIR}/conf/ -name fullchain1.pem -exec chmod 644 {} \; \
    && find ${CERTBOTDIR}/conf/ -name chain.pem -exec chmod 644 {} \; \
    && find ${CERTBOTDIR}/conf/ -name chain1.pem -exec chmod 644 {} \; \
    && find ${CERTBOTDIR}/conf/ -name privkey.pem -exec chmod 644 {} \; \
    && find ${CERTBOTDIR}/conf/ -name privkey1.pem -exec chmod 644 {} \; 

if grep -q "Congratulations! Your certificate" "log.txt"; then
    echo "certificate was renewed"
    echo "curl nginx configuration" \
    && curl https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf > ${CERTBOTDIR}/conf/options-ssl-nginx.conf \
    && find ${CERTBOTDIR}/conf/ -name options-ssl-nginx.conf -exec chmod 644 {} \; \
    && echo "generating dhparam.pem" \
    && openssl dhparam -out ${CERTBOTDIR}/conf/dhparam.pem 2048 \
    && find ${CERTBOTDIR}/conf/ -name dhparam.pem -exec chmod 644 {} \; \
    && echo "reloading docker containers" \
    && docker-compose -f docker-compose.yml restart
else
    echo "certificate was not renewed, cleaning up now"
fi
mv log.txt lastlog.txt
docker container stop nginx-letsencrypt
