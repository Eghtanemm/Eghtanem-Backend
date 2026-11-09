<?php

namespace App\Http\Controllers\Auth;

use App\Http\Controllers\Controller;
use App\Http\Requests\SocialLoginRequest;
use App\Models\User;
use Carbon\Carbon;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Str;
use Kreait\Firebase\Contract\Auth as FirebaseAuth;
use Kreait\Firebase\Exception\Auth\FailedToVerifyToken;

class SocialAuthController extends Controller
{
    public function __construct(protected FirebaseAuth $firebaseAuth) {}

    public function handle(SocialLoginRequest $request): JsonResponse
    {
        $idTokenString = $request->validated('id_token');
        $provider = $request->validated('provider');
        $requestName = $request->validated('name');

        // 1. Validate the Firebase ID token
        try {
            $verifiedIdToken = $this->firebaseAuth->verifyIdToken($idTokenString);
        } catch (FailedToVerifyToken $e) {
            return response()->json([
                'message' => 'Invalid or expired Firebase token.',
                'error'   => $e->getMessage(),
            ], 401);
        }

        // 2. Extract user data from the claims
        $claims = $verifiedIdToken->claims();
        $firebaseUid = $claims->get('sub');
        $email = $claims->get('email');
        $tokenName = $claims->get('name');
        $emailVerified = $claims->get('email_verified') === true;

        // Name handling: prioritize the name from the request (important for Apple), then the token, then a default name
        $resolvedName = !empty($requestName)
            ? $requestName
            : ($tokenName ?: ($email ? explode('@', $email)[0] : 'User'));

        // 3. Find or create the user
        // Search first by firebase_uid, then by email to link the account if it already exists
        $user = User::where('firebase_uid', $firebaseUid)->first();

        if (!$user && $email) {
            if (!$emailVerified) {
                return response()->json([
                    'message' => 'The email address is not verified by the provider.',
                ], 403);
            }

            $user = User::where('email', $email)->first();
        }

        if ($user) {
            // Update provider and UID data if the account was not already registered
            $user->update([
                'firebase_uid' => $firebaseUid,
                'provider'     => $provider,
                'name'         => $user->name ?: $resolvedName,
            ]);
        } else {
            // Create a new user
            $user = User::create([
                'name'              => $resolvedName,
                'email'             => $email,
                'firebase_uid'      => $firebaseUid,
                'provider'          => $provider,
                'email_verified_at' => now(),
                'password'          => bcrypt(Str::random(32)),
                'email_verified' => true
            ]);
        }

        // 4. Generate the tokens (example using Laravel Passport or Sanctum with expiration dates)
        // Adjust the following token generation according to the authentication package used in your project
        $accessTokenExpiresAt = Carbon::now()->addDays(30);
        $refreshTokenExpiresAt = Carbon::now()->addDays(60);

        $tokenResult = $user->createToken('SocialLoginToken');
        $accessToken = $tokenResult->plainTextToken ?? 'app-access-token';
        $refreshToken = Str::random(64); // or use the refresh token provided by Passport

        // 5. Return the JSON response that matches the exact structure requested by the filter developer in the design
        return response()->json([
            'access_token'            => $accessToken,
            'refresh_token'           => $refreshToken,
            'access_token_expires_at' => $accessTokenExpiresAt->toISOString(true),
            'refresh_token_expires_at' => $refreshTokenExpiresAt->toISOString(true),
            'user' => [
                'id'    => (int) $user->id,
                'name'  => (string) $user->name,
                'email' => (string) ($user->email ?? ''),
            ],
        ], 200);
    }
}
