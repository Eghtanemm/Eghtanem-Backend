<?php

use App\Http\Controllers\Auth\LoginController;
use App\Http\Controllers\UploadVideoController;
use Illuminate\Support\Facades\Response;
use Illuminate\Support\Facades\Route;
use Illuminate\Support\Facades\Storage;

Route::get('/', function () {
    return view('welcome');
});

Route::post('/login', [LoginController::class, 'registerUser'])->name('register.custom');

Route::post('/verify-otp', [LoginController::class, 'verifyOtp'])->name('verify.otp');

Route::get('/upload-video', [UploadVideoController::class, 'showForm'])->name('upload.video.form');
Route::post('/upload-video', [UploadVideoController::class, 'store'])->name('upload.video.store');

Route::get('/video/{filename}', function ($filename) {
    $foundPath = null;

    // نفحص accepted videos مباشرة
    $acceptedPath = 'accepted-videos/'.$filename;
    if (Storage::exists($acceptedPath)) {
        $foundPath = $acceptedPath;
    }

    // لو مش موجود في accepted، ندور جوه كل مجلدات pending-videos
    if (! $foundPath) {
        $folders = Storage::directories('pending-videos');

        foreach ($folders as $folder) {
            $fullPath = $folder.'/'.$filename;
            if (Storage::exists($fullPath)) {
                $foundPath = $fullPath;
                break;
            }
        }
    }

    if (! $foundPath) {
        abort(404);
    }

    return Response::file(Storage::path($foundPath));
})->where('filename', '.*');
