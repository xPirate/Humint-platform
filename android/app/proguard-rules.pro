# SQLCipher loads its native library by name through JNI; R8 has no way to
# see that and will happily strip the binding.
-keep class net.zetetic.database.** { *; }
-keep class net.sqlcipher.** { *; }

# Room generates implementations reflectively referenced by the generated
# database class.
-keep class * extends androidx.room.RoomDatabase { <init>(); }

# OkHttp's optional platform integrations are referenced but not present;
# without these R8 warns on every release build.
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
