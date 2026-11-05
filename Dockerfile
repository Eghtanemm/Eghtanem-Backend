FROM laravelsail/php84-composer:latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu-dev \
    && docker-php-ext-install intl \
    && rm -rf /var/lib/apt/lists/*

# `php artisan serve` runs the CLI server, which ignores public/.user.ini,
# so upload limits must be set in the global PHP config.
RUN printf "upload_max_filesize=256M\npost_max_size=256M\nmemory_limit=512M\n" \
    > /usr/local/etc/php/conf.d/uploads.ini
