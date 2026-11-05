<?php

use App\Http\Controllers\Auth\LoginController;
use Illuminate\Support\Facades\Route;

Route::get('/', function () {
    return view('welcome');
});

Route::post('/login', [LoginController::class, 'registerUser'])->name('register.custom');

Route::post('/verify-otp', [LoginController::class, 'verifyOtp'])->name('verify.otp');
Route::get('/test-mail', function () {
    \Illuminate\Support\Facades\Mail::raw('اختبار SMTP من Laravel', function ($msg) {
        $msg->to('dawa.platform@gmail.com')->subject('Test Mail');
    });

    return 'تم الإرسال ✅';
});
