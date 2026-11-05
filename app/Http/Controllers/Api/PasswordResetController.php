<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Models\PasswordResetCode;
use App\Models\User;
use App\Notifications\ResetPasswordNotification;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Facades\Validator;
use Illuminate\Support\Str;

/**
 * @OA\Tag(
 *     name="Password Reset",
 *     description="API Endpoints for password reset flow"
 * )
 */
class PasswordResetController extends Controller
{
    /**
     * @OA\Post(
     *     path="/api/forgot-password",
     *     summary="Request password reset verification code",
     *     tags={"Password Reset"},
     *
     *     @OA\RequestBody(
     *         required=true,
     *
     *         @OA\JsonContent(
     *             required={"email"},
     *
     *             @OA\Property(property="email", type="string", format="email")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=200,
     *         description="Verification code sent successfully",
     *
     *         @OA\JsonContent(
     *
     *             @OA\Property(property="message", type="string", example="Verification code sent to your email")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=422,
     *         description="Validation error"
     *     )
     * )
     */
    public function sendCode(Request $request): JsonResponse
    {
        $validator = Validator::make($request->all(), [
            'email' => 'required|email|exists:users,email',
        ]);

        if ($validator->fails()) {
            return response()->json(['errors' => $validator->errors()], 422);
        }

        $user = User::where('email', $request->email)->first();
        $resetCode = PasswordResetCode::createCodeForEmail($request->email);
        $user->notify(new ResetPasswordNotification($resetCode->code));

        return response()->json(['message' => 'Verification code sent to your email']);
    }

    /**
     * @OA\Post(
     *     path="/api/verify-reset-code",
     *     summary="Verify the reset code and get reset token",
     *     tags={"Password Reset"},
     *
     *     @OA\RequestBody(
     *         required=true,
     *
     *         @OA\JsonContent(
     *             required={"email", "code"},
     *
     *             @OA\Property(property="email", type="string", format="email"),
     *             @OA\Property(property="code", type="string", example="123456", description="6-digit verification code")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=200,
     *         description="Code verified successfully",
     *
     *         @OA\JsonContent(
     *
     *             @OA\Property(property="message", type="string", example="Code verified successfully"),
     *             @OA\Property(property="reset_token", type="string")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=422,
     *         description="Invalid or expired code"
     *     )
     * )
     */
    public function verifyCode(Request $request): JsonResponse
    {
        $validator = Validator::make($request->all(), [
            'email' => 'required|email',
            'code' => 'required|string|size:6',
        ]);

        if ($validator->fails()) {
            return response()->json(['errors' => $validator->errors()], 422);
        }

        $resetCode = PasswordResetCode::where('email', $request->email)
            ->where('code', $request->code)
            ->where('used', false)
            ->latest()
            ->first();

        if (! $resetCode || ! $resetCode->isValid()) {
            return response()->json(['message' => 'Invalid or expired code'], 422);
        }

        // Generate a reset token and store it
        $token = Str::random(60);
        $resetCode->markUsedWithToken($token);

        return response()->json([
            'message' => 'Code verified successfully',
            'reset_token' => $token,
        ]);
    }

    /**
     * @OA\Post(
     *     path="/api/reset-password",
     *     summary="Reset password using the reset token",
     *     tags={"Password Reset"},
     *
     *     @OA\RequestBody(
     *         required=true,
     *
     *         @OA\JsonContent(
     *             required={"email", "reset_token", "password", "password_confirmation"},
     *
     *             @OA\Property(property="email", type="string", format="email"),
     *             @OA\Property(property="reset_token", type="string"),
     *             @OA\Property(property="password", type="string", format="password", minLength=8),
     *             @OA\Property(property="password_confirmation", type="string", format="password")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=200,
     *         description="Password reset successfully",
     *
     *         @OA\JsonContent(
     *
     *             @OA\Property(property="message", type="string", example="Password reset successfully")
     *         )
     *     ),
     *
     *     @OA\Response(
     *         response=422,
     *         description="Invalid token or validation error"
     *     ),
     *     @OA\Response(
     *         response=404,
     *         description="User not found"
     *     )
     * )
     */
    public function resetPassword(Request $request): JsonResponse
    {
        $validator = Validator::make($request->all(), [
            'email' => 'required|email',
            'reset_token' => 'required|string',
            'password' => 'required|string|min:8|confirmed',
        ]);

        if ($validator->fails()) {
            return response()->json(['errors' => $validator->errors()], 422);
        }

        return DB::transaction(function () use ($request) {
            // Lock the user so concurrent resets for this account cannot reuse a token
            $user = User::where('email', $request->email)->lockForUpdate()->first();

            // Verify the reset token is valid
            $resetCode = PasswordResetCode::where('email', $request->email)
                ->where('reset_token', $request->reset_token)
                ->where('used', true)
                ->where('expires_at', '>', now())
                ->latest()
                ->first();

            if (! $resetCode) {
                return response()->json(['message' => 'Invalid or expired reset token'], 422);
            }

            if (! $user) {
                return response()->json(['message' => 'User not found'], 404);
            }

            $user->password = Hash::make($request->password);
            $user->refresh_token = null;
            $user->refresh_token_expires_at = null;
            $user->access_token_expires_at = null;
            $user->save();

            $user->tokens()->delete();

            // "used" only means the code was verified, so deleting is what consumes the token
            PasswordResetCode::where('email', $request->email)->delete();

            return response()->json(['message' => 'Password reset successfully']);
        });
    }
}
