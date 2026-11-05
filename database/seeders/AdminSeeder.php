<?php

namespace Database\Seeders;

use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\Hash;
use RuntimeException;

class AdminSeeder extends Seeder
{
    /**
     * Seed an admin user from the explicitly configured credentials.
     */
    public function run(): void
    {
        $name = env('ADMIN_NAME', 'Admin User');
        $email = env('ADMIN_EMAIL');
        $password = env('ADMIN_PASSWORD');

        if (blank($email) || blank($password)) {
            throw new RuntimeException(
                'AdminSeeder requires ADMIN_EMAIL and ADMIN_PASSWORD to be set. '
                .'In production, do not seed: create admins with php artisan app:create-filament-admin.'
            );
        }

        User::query()->updateOrCreate(
            ['email' => $email],
            [
                'name' => $name,
                'password' => Hash::make($password),
                'role' => 'admin',
                'email_verified_at' => now(),
            ],
        );
    }
}
