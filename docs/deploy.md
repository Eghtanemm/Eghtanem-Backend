# Deploying E8tanem on aaPanel

This guide provides simple steps to deploy the E8tanem Laravel application on aaPanel.

## Pre-requisites

- An aaPanel installation on your server
- A domain name pointing to your server
- SSH access to your server

## Step 1: Install Required Software on aaPanel

1. Log in to your aaPanel dashboard
2. Go to the "Software Store" section
3. Install the following:
   - Nginx (or Apache)
   - PHP 8.1+ (with extensions: BCMath, Ctype, Fileinfo, JSON, Mbstring, OpenSSL, PDO, Tokenizer, XML)
   - MySQL (or MariaDB)
   - Redis (optional, for caching)
   - Composer 2

## Step 2: Create a Database

1. Go to the "Database" section in aaPanel
2. Create a new MySQL database and user
3. Note down the database name, username, and password

## Step 3: Set Up Website in aaPanel

1. Go to the "Website" section in aaPanel
2. Click "Add Site"
3. Enter your domain name
4. Set the document root to the `/public` folder of your Laravel application
5. Enable HTTPS (using Let's Encrypt)

## Step 4: Upload the Application

Method 1: Using Git (Recommended)
```bash
# Connect to your server via SSH
ssh user@your-server-ip

# Navigate to the site directory (depends on your aaPanel configuration)
cd /www/wwwroot/yourdomain.com

# Clone your repository (replace with your actual Git URL)
git clone https://your-git-repository-url.git .

# If your public folder is already in the document root, you may need to move files
```

Method 2: Using FTP/SFTP
1. Use an FTP client like FileZilla to upload your application files
2. Upload to the website root directory configured in aaPanel

## Step 5: Configure the Application

1. Create an `.env` file in your application root:
```
APP_NAME="E8tanem"
APP_ENV=production
APP_KEY=
APP_DEBUG=false
APP_URL=https://yourdomain.com

LOG_CHANNEL=stack
LOG_LEVEL=warning

DB_CONNECTION=mysql
DB_HOST=localhost
DB_PORT=3306
DB_DATABASE=your_database_name
DB_USERNAME=your_database_user
DB_PASSWORD=your_database_password

BROADCAST_DRIVER=log
CACHE_DRIVER=file
FILESYSTEM_DISK=public
QUEUE_CONNECTION=sync
SESSION_DRIVER=file
SESSION_LIFETIME=120

# Add other necessary configuration settings
```

2. Generate application key:
```bash
php artisan key:generate
```

3. Run migrations:
```bash
php artisan migrate --force
```

> **Warning:** do not run `--seed` or `db:seed` in production. Create the admin user in Step 9 instead.

4. Set proper permissions:
```bash
chmod -R 755 .
chmod -R 777 storage
chmod -R 777 bootstrap/cache
```

## Step 6: Configure Nginx/Apache for Laravel

In aaPanel:
1. Go to the "Website" section
2. Click on your domain's "Settings"
3. Go to "Rewrite Rules"
4. If using Nginx, add a Laravel rewrite rule (use the built-in template for Laravel)
5. If using Apache, make sure the `.htaccess` file is properly configured

## Step 7: Set Up Symbolic Link for Storage

```bash
php artisan storage:link
```

## Step 8: Optimize for Production

```bash
php artisan config:cache
php artisan route:cache
php artisan view:cache
php artisan optimize
```

## Step 9: Create Admin User

The command prompts for the password, which keeps it out of your shell history:

```bash
php artisan app:create-filament-admin "Admin Name" admin@example.com
```

## Step 10: Final Checks

1. Visit your website to ensure it loads correctly
2. Test the login functionality
3. Try accessing the admin panel at `/admin`
4. Check that file uploads work correctly

## Troubleshooting

### Common Issues

1. **500 Server Error**:
   - Check permissions on storage and bootstrap/cache folders
   - Check your .env configuration
   - Review the Laravel logs in storage/logs

2. **404 Not Found for all routes**:
   - Verify your Nginx/Apache rewrite rules
   - Ensure the document root points to the public directory

3. **Database Connection Error**:
   - Double-check your database credentials in .env
   - Ensure the MySQL service is running

### aaPanel Specific Tips

- Use the "Task" section in aaPanel to set up cron jobs for Laravel Scheduler:
  ```
  * * * * * cd /path-to-your-project && php artisan schedule:run >> /dev/null 2>&1
  ```

- If using Redis, configure it in your .env file:
  ```
  CACHE_DRIVER=redis
  REDIS_HOST=127.0.0.1
  REDIS_PASSWORD=null
  REDIS_PORT=6379
  ```

## Maintenance

Refer to the [Production Checklist](production-checklist.md) for guidance on regular maintenance and additional security measures.
