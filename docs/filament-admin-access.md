# Filament Admin Access Control

This document explains how the admin access control system works in this application.

## Overview

In our application, Filament admin panel access is restricted:
- Only users with `role = 'admin'` can access the admin panel
- Regular users (`role = 'user'`) will receive a 403 Forbidden error when trying to access the admin panel
- This restriction is applied in all environments, including development

## How It Works

1. **AdminMiddleware**: This middleware checks if the current user has admin privileges:
   ```php
   // app/Http/Middleware/AdminMiddleware.php
   public function handle(Request $request, Closure $next): Response
   {
       // Check if user is logged in and is an admin
       if (!$request->user() || !$request->user()->isAdmin()) {
           abort(403, 'Unauthorized access.');
       }
       return $next($request);
   }
   ```

2. **User Model**: The User model has a `role` field and an `isAdmin()` method:
   ```php
   // app/Models/User.php
   public function isAdmin(): bool
   {
       return $this->role === 'admin';
   }
   ```

3. **Environmental Control**: The middleware is applied in all environments:
   ```php
   // app/Providers/Filament/AdminPanelProvider.php
   ->authMiddleware([
       Authenticate::class,
       \App\Http\Middleware\AdminMiddleware::class,
   ])
   ```

## Managing Admin Users

### Creating Admin Users

Users created with the `make:filament-user` command are automatically assigned the admin role. There are two ways to create admin users:

1. Using the Filament built-in command:
   ```bash
   php artisan make:filament-user
   ```

2. Using our custom command which gives more detailed feedback:
   ```bash
   php artisan app:test-filament-user "Admin Name" admin@example.com your-strong-password
   ```

3. Using our dedicated admin creation command:
   ```bash
   php artisan app:create-filament-admin "Admin Name" admin@example.com
   ```

### Checking Admin Status

```php
$user = User::find(1);
if ($user->isAdmin()) {
    // User is an admin
}
```

### Command for Promoting Users

You can use our custom Artisan command to promote a user to admin:

```bash
php artisan app:promote-user-to-admin user@example.com
```

### Manual Database Updates

You can also update the database manually:

```php
$user = User::where('email', 'user@example.com')->first();
$user->role = 'admin';
$user->save();
```

## Testing Admin Access

We have tests for admin access in `tests/Feature/FilamentAdminAccessTest.php` that verify:
1. Non-admin users cannot access the admin panel
2. Admin users can access the admin panel

Run these tests with:

```bash
php artisan test --filter=FilamentAdminAccessTest
```

## Production Considerations

- All users default to `role = 'user'` when registering
- The `role` field is included in the users table
- Only promote trusted users to admin
- Consider adding activity logging for admin actions in production